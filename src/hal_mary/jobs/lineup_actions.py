"""The bye-week bench: the first recommendation hal-mary turns into an action.

A player whose real team does not play this week scores exactly zero. Nobody
intends it, the fix is one click, and the click is fully reversible. That is why
it goes first: it is the one recommendation concrete enough to hand to an
executor with no judgement, and wiring it end to end proves the loop before
anything irreversible travels down it.

**Deterministic, with no Claude call.** The reasoning here is arithmetic — who is
in a starting slot, whose bye week equals this week, who on the bench can legally
fill the slot they leave. An action that fires without a model is one less thing
that can go wrong at eleven o'clock on a Sunday morning, and there is nothing
here a model would do better.

Three refusals matter as much as the emission:

* **No legal replacement, no action.** A bench with nobody to start in the empty
  slot is worse than the bye: zero either way, and now the lineup is short. The
  job writes a note saying so instead, because "we looked and could not" is a
  thing next week's reasoning wants to know.
* **An unknown bye is not a bye.** Bye weeks come from the researched board, and
  a player with no board row has no known bye. Guessing benches somebody who
  plays.
* **An unknown week does nothing at all.** ``league_settings.current_week`` is
  written by the ESPN sync; before the first sync there is no "this week" and
  every comparison would be against ``None``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from hal_mary import actions, db, memory
from hal_mary.config import ConfigError, Settings
from hal_mary.league import LeagueUnknown, load_league_context

__all__ = ["JOB_NAME", "SLOT_ELIGIBILITY", "emit_bye_week_benchings", "refresh_after_sync"]

log = logging.getLogger(__name__)

#: This job has no ``[jobs.*]`` entry in config.toml, and that is deliberate:
#: it spawns no ``claude`` binary, so it has no model, no tool list, no timeout
#: and no budget to tune. There is nothing about it to configure.
JOB_NAME = "lineup_actions"

#: Which positions may fill a multi-position lineup slot. Slots not listed here
#: take their own name — a QB slot takes a QB. ESPN's own spellings, from
#: ``espn_api.football.constant.POSITION_MAP``; this league's flex slot is
#: spelled ``RB/WR/TE`` and the string "FLEX" appears nowhere on her screen.
SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "RB/WR": frozenset({"RB", "WR"}),
    "WR/TE": frozenset({"WR", "TE"}),
    "RB/WR/TE": frozenset({"RB", "WR", "TE"}),
    "OP": frozenset({"QB", "RB", "WR", "TE"}),
    "TQB": frozenset({"QB"}),
}

#: The bench. A player here is not starting, so his bye costs nothing.
BENCH_SLOTS = frozenset({"BE", "BN", "BENCH"})

#: Injury statuses that still let a player take the field. Everything else —
#: OUT, DOUBTFUL, SUSPENSION, INJURY_RESERVE — makes him no better than the bye
#: he is being asked to cover.
STARTABLE_INJURY_STATUSES = frozenset({"ACTIVE", "NORMAL", "QUESTIONABLE", "PROBABLE", ""})

#: Nothing ranks below this, so a bench player the board never ranked sorts
#: behind every player it did rather than ahead of them.
_UNRANKED = 10**9


def _roster(conn: sqlite3.Connection, team_id: int) -> list[dict[str, Any]]:
    """Her current roster, with the board row that carries each bye week.

    ``roster_slots`` holds the current snapshot with a NULL week; the board is
    the only place a bye week is stored, and a LEFT JOIN keeps a player the
    board never carried rather than dropping him out of the lineup entirely.
    """
    rows = conn.execute(
        """
        SELECT p.player_id, p.name, p.position, p.injury_status, r.slot, b.bye_week, b.rank
          FROM roster_slots r
          JOIN players p ON p.player_id = r.player_id
          LEFT JOIN board b ON b.player_id = r.player_id
         WHERE r.team_id = ? AND r.week IS NULL
         ORDER BY p.name
        """,
        (team_id,),
    )
    return [dict(row) for row in rows]


def _eligible_positions(slot: str) -> frozenset[str]:
    return SLOT_ELIGIBILITY.get(slot.upper(), frozenset({slot.upper()}))


def _is_startable(player: dict[str, Any], week: int) -> bool:
    """Would this player actually score if he were started this week?"""
    if player.get("bye_week") == week:
        return False
    status = (player.get("injury_status") or "").strip().upper()
    return status in STARTABLE_INJURY_STATUSES


def _replacement_sort_key(player: dict[str, Any]) -> tuple[int, str]:
    """Best first: board rank, then name so two runs never disagree."""
    rank = player.get("rank")
    return (int(rank) if rank is not None else _UNRANKED, player.get("name") or "")


def _reason(player: str, replacement: str, slot: str, week: int) -> str:
    """One sentence, for a person who knows the rules of football and no more.

    It is what Bryan reads in the call log and what Caroline reads on the
    dashboard, and it is never reasoning for Cowork to interpret.
    """
    return (
        f"{player}'s team does not play in week {week}, so he scores nothing where he is — "
        f"put {replacement} in the {slot} slot instead."
    )


def emit_bye_week_benchings(
    conn: sqlite3.Connection, settings: Settings, *, now: Any = None
) -> dict[str, Any]:
    """Emit one ``bench`` action per started player who is on bye this week.

    Each action names the player to sit, the slot he vacates, and the benched
    player who takes it — one instruction, not two that would briefly leave the
    lineup a man short.

    Returns ``{"week", "emitted", "unreplaceable", "summary"}``. Running it twice
    emits once: :func:`hal_mary.actions.emit` refuses an equivalent action that
    is already pending or done.
    """
    run_id = db.job_run_started(conn, JOB_NAME)
    try:
        outcome = _plan(conn, settings, now=now)
    except Exception as exc:
        db.job_run_finished(conn, run_id, "error", error=str(exc))
        raise
    db.job_run_finished(conn, run_id, "ok", summary=outcome["summary"])
    return outcome


def _nothing(week: int | None, summary: str) -> dict[str, Any]:
    return {
        "week": week,
        "emitted": [],
        "already_queued": [],
        "unreplaceable": [],
        "summary": summary,
    }


def _plan(conn: sqlite3.Connection, settings: Settings, *, now: Any = None) -> dict[str, Any]:
    try:
        league = load_league_context(conn, settings)
    except LeagueUnknown as exc:
        # Not fatal. Before the first sync there is no league to reason about,
        # and a job that raises here would show as a failed run every hour on a
        # box that is simply new.
        return _nothing(None, f"League settings are unknown, so nothing was checked: {exc}")

    week = league.current_week
    if week is None:
        return _nothing(
            None,
            "ESPN has not told us which week it is yet, so no bye could be checked. "
            "Run `hal-mary sync`.",
        )

    # Resolved before the roster is even read, so a misconfigured boundary is an
    # error on every run rather than only on the weeks that had work to do. Every
    # action emitted below expires when this NFL week does. It is a coarse
    # deadline — the exact one is that player's own kickoff, which needs a pro
    # schedule hal-mary does not sync yet — but coarse is the difference between
    # an instruction that goes stale and one that does not. Without it, an
    # executor that was offline for a fortnight comes back in week 7 and benches
    # a healthy starter for a bye that ended three weeks ago, and nothing
    # anywhere would have revoked it.
    deadline = actions.end_of_nfl_week(now, settings)

    starting_slots = {slot.upper() for slot in league.starting_slots}
    roster = _roster(conn, league.my_team_id)
    if not roster:
        return _nothing(week, f"Her roster is empty, so there is nothing to check in week {week}.")

    started_on_bye = [
        player
        for player in roster
        if (player["slot"] or "").upper() in starting_slots
        and player["bye_week"] == week
    ]
    bench = [
        player
        for player in roster
        if (player["slot"] or "").upper() in BENCH_SLOTS and _is_startable(player, week)
    ]
    # Deterministic order for the plan: by the slot the player sits in, then by
    # name, so two runs over the same roster produce the same sequence numbers.
    started_on_bye.sort(key=lambda player: ((player["slot"] or ""), player["name"] or ""))

    emitted: list[int] = []
    already_queued: list[int] = []
    unreplaceable: list[str] = []
    taken: set[int] = set()
    sequence = 0

    for player in started_on_bye:
        slot = player["slot"] or ""
        allowed = _eligible_positions(slot)
        candidates = [
            candidate
            for candidate in bench
            if candidate["player_id"] not in taken
            and (candidate["position"] or "").upper() in allowed
        ]
        if not candidates:
            unreplaceable.append(player["name"])
            continue
        replacement = min(candidates, key=_replacement_sort_key)
        taken.add(replacement["player_id"])
        sequence += 1
        proposed = actions.Action(
            kind="bench",
            player_name=player["name"],
            player_id=player["player_id"],
            slot=slot,
            paired_player_name=replacement["name"],
            reason=_reason(player["name"], replacement["name"], slot, week),
            sequence=sequence,
            # Every one of these is independent: benching one player on bye
            # neither needs nor blocks benching another.
            depends_on=(),
            deadline=deadline,
            reversible=True,
            source_job=JOB_NAME,
        )
        # Asked before emitting, not inferred from the id afterwards: `emit`
        # returns an id whether it wrote a row or found one, and a run that
        # cannot tell "already queued" from "newly decided" cannot say honestly
        # what it did.
        existing = actions.find_equivalent(conn, proposed, now=now, settings=settings)
        action_id = actions.emit(conn, proposed, now=now, settings=settings)
        (already_queued if existing is not None else emitted).append(action_id)

    _note_the_unreplaceable(conn, unreplaceable, week)

    if emitted:
        summary = f"Week {week}: queued a bench for {len(emitted)} player(s) on bye."
    elif already_queued:
        summary = f"Week {week}: {len(already_queued)} bye bench(es) already queued this week."
    elif unreplaceable:
        summary = f"Week {week}: {len(unreplaceable)} player(s) on bye with nobody legal to start."
    else:
        summary = f"Week {week}: nobody in her lineup is on bye."
    return {
        "week": week,
        "emitted": emitted,
        "already_queued": already_queued,
        "unreplaceable": unreplaceable,
        "summary": summary,
    }


def _note_the_unreplaceable(conn: sqlite3.Connection, names: list[str], week: int) -> None:
    """Record every bye we could not cover, once each.

    "We looked and there was nobody legal" is exactly what the waiver reasoning
    wants next: an empty starting slot is a position she needs to add. Written
    once per player per week — a job that runs hourly must not fill the notes
    table with the same sentence.
    """
    for name in names:
        already = conn.execute(
            """
            SELECT 1 FROM notes
             WHERE source_job = ? AND player_name = ? AND topic = ?
             LIMIT 1
            """,
            (JOB_NAME, name, _bye_topic(week)),
        ).fetchone()
        if already:
            continue
        memory.write_note(
            conn,
            memory.Note(
                text=(
                    f"{name} is on bye in week {week} and is in her starting lineup, but "
                    "nobody on her bench can legally fill that slot, so he was left there."
                ),
                source_job=JOB_NAME,
                topic=_bye_topic(week),
                player_name=name,
            ),
        )


def _bye_topic(week: int) -> str:
    return f"uncovered-bye-week-{week}"


def refresh_after_sync(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any] | None:
    """Recompute the plan after a sync, and expire anything that went stale.

    A sync is the moment the roster and the week both change, which is exactly
    when a bye-week bench becomes true or stops being true — so this is the
    trigger, rather than a scheduler this application does not have yet.

    A producer failure is swallowed: the sync's real payload is the roster, the
    free agents and the memory file, and a plan that could not be built must not
    turn a good sync into a failed one. It is logged and ``None`` comes back.

    **A configuration error is not swallowed.** A ``ConfigError`` means the
    deployment cannot work at all, and hiding it produces "the plan silently
    stops refreshing" — a symptom with no message, on a box where nobody is
    watching. The league data is already committed by the time this runs, so
    letting it through costs a red sync row that names the wrong config key and
    nothing else.
    """
    try:
        actions.expire_stale(conn)
        return emit_bye_week_benchings(conn, settings)
    except ConfigError:
        raise
    except Exception:  # deliberately everything else; see the docstring
        log.exception("refreshing the action plan after a sync failed")
        return None
