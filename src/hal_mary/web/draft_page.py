"""Everything the draft page shows, assembled from the database.

This is the screen Caroline reads on draft night with ESPN open beside it and a
90-second clock running, so the shape of the page is an argument about
attention, not about data:

1. **the advice card** — the one thing she will read if she reads one thing;
2. **whose pick it is** — the question asked most often during a draft;
3. **her roster so far**, with the empty slots visible as empty slots;
4. **the board**;
5. **manual pick entry**, collapsed, with any disagreement between the board and
   reality sitting immediately above it.

Three things here are easy to get subtly wrong, and each one is a wrong
recommendation rather than an ugly page.

**A fallback card must not look like a researched one.** ``attempts == 0`` means
no model call was made at all: the recommendation is the ranking list's own
answer, computed from tiers. She cannot weigh advice she cannot tell apart, so
the two cards are different objects on the page, not the same card with a
footnote.

**Advice for an older pick is not current advice.** The card carries the pick it
was written for; when the draft has moved on, the page says so rather than
letting a confident sentence about a player who went four picks ago read as
today's answer.

**An unmatched pick means the board and reality disagree about who is gone.**
That is the one condition that makes a recommendation actively wrong rather than
merely unhelpful, and the fix is a human typing a name in — so it renders beside
the manual-entry control, never only in a log.

Nothing in this module raises for a database that is empty, half-synced or
missing a league. Every one of those is a real state on draft morning, and each
one gets a sentence.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from hal_mary.config import Settings
from hal_mary.draft import board as board_math
from hal_mary.draft import store
from hal_mary.espn.sync import last_sync
from hal_mary.league import LeagueContext, LeagueUnknown, load_league_context
from hal_mary.web.positions import (
    POSITION_WORDS,
    position_plural,
    position_word,
    slot_label,
    slot_sort_key,
)

__all__ = ["draft_context", "seconds_since", "spell_out_age"]

log = logging.getLogger(__name__)

#: How many still-available players the board shows. A display size, not a
#: budget: long enough that a run on one position does not empty the list, short
#: enough to thumb past on a phone.
BOARD_ROWS = 30

#: How many just-taken players are shown struck through under the board. The
#: page's answer to "who went while I was looking at ESPN" — a handful, because
#: the whole list of gone players is not a thing anybody reads mid-draft.
GONE_ROWS = 6

#: Positions offered as board filters, in the order they matter to a drafter.
#: Anything on the board that is not listed is appended, so an unusual league
#: still gets a filter rather than a silently missing one.
FILTER_ORDER = ("RB", "WR", "TE", "QB", "K", "D/ST")

#: Slots nobody drafts into, so nobody needs a row for one.
_NOT_DRAFTED = frozenset({"IR", "RES", "TAXI", "IR/RES"})

#: Below this many seconds an age is spelled out in seconds. Above it,
#: ``age_in_words`` is more readable. 120 rather than 60 so the stale banner —
#: which fires in the tens of seconds — always shows a number she can act on.
_SECONDS_PHRASE_LIMIT = 120


# --- small helpers -----------------------------------------------------------


def seconds_since(stamp: str | None, now: datetime | None = None) -> float | None:
    """How long ago ``stamp`` was, in seconds. ``None`` when it cannot be read.

    The draft page needs this where the status page needs ``age_in_words``: a
    sync that is 40 seconds behind is a problem during a draft and "just now"
    everywhere else.
    """
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - moment).total_seconds())


def spell_out_age(seconds: float | None) -> str:
    """An age in words, precise enough to act on during a draft."""
    if seconds is None:
        return "never"
    whole = int(seconds)
    if whole < _SECONDS_PHRASE_LIMIT:
        return f"{whole} second{'' if whole == 1 else 's'} ago"
    minutes = whole // 60
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:  # pragma: no cover - an unmigrated database
        return []


# --- the pieces --------------------------------------------------------------


def _teams(conn: sqlite3.Connection) -> dict[int, str]:
    """Team names by id, for "whose pick is it" and the manual-entry selector."""
    return {
        int(row["team_id"]): (row["name"] or f"Team {row['team_id']}")
        for row in _rows(
            conn,
            "SELECT team_id, name FROM teams"
            " ORDER BY CASE WHEN draft_slot IS NULL THEN 1 ELSE 0 END, draft_slot, team_id",
        )
    }


def _turn(
    league: LeagueContext | None,
    teams: dict[int, str],
    next_pick: int,
) -> dict[str, Any]:
    """Whose pick it is, and how many until hers.

    **Gated on ``my_upcoming_picks`` being non-empty.** ``picks_until_mine``
    treats the draft as running forever, so without the gate this page counts
    down past the last pick of the draft and keeps telling her to get ready.
    """
    if league is None:
        return {"known": False, "over": False, "next_overall_pick": next_pick}

    upcoming = league.upcoming_picks(next_pick)
    if not upcoming:
        return {"known": True, "over": True, "next_overall_pick": next_pick}

    until = league.picks_until_mine(next_pick)
    on_the_clock = board_math.pick_slot(next_pick, league.draft_order, league.snake)
    return {
        "known": True,
        "over": False,
        "next_overall_pick": next_pick,
        "round_num": (next_pick - 1) // league.team_count + 1,
        "total_rounds": league.rounds,
        "on_the_clock_team_id": on_the_clock,
        "on_the_clock": teams.get(on_the_clock, f"Team {on_the_clock}"),
        "mine_now": until == 0,
        "picks_until_mine": until,
        "my_next_picks": upcoming[:3],
        "started": next_pick > 1,
    }


def _latest_advice(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = _rows(
        conn,
        "SELECT id, created_at, payload_json FROM advice WHERE kind = 'draft'"
        " ORDER BY id DESC LIMIT 1",
    )
    if not row:
        return None
    try:
        payload = json.loads(row[0]["payload_json"] or "{}")
    except (TypeError, ValueError):
        log.warning("the newest advice row holds unreadable JSON; showing no card")
        return None
    if not isinstance(payload, dict) or not payload.get("pick"):
        return None
    payload["created_at"] = row[0]["created_at"]
    return payload


def _written_for(payload: dict[str, Any]) -> int | None:
    """Which of *her* picks this card is advice for.

    **Not ``next_overall_pick``.** That field is the pick that was on the clock
    when the advisor ran, and the whole design is that it runs early: the loop
    fires as soon as she is within ``draft.advise_within_picks``, so a card for
    her pick 6 is normally written while pick 4 is on the clock, and then
    ``_last_advised_pick`` stops it being written again. Labelling the card with
    the pick it was written *on* makes a correct, current recommendation
    announce that it is out of date and promise a replacement that no code will
    ever write — on her turn, every turn.

    ``my_next_picks[0]`` is the pick the advisor actually reasoned about.
    ``next_overall_pick`` is kept only as a fallback for a row written before
    this was understood.
    """
    upcoming = payload.get("my_next_picks") or []
    if upcoming:
        try:
            return int(upcoming[0])
        except (TypeError, ValueError):  # pragma: no cover - a corrupt row
            pass
    return payload.get("next_overall_pick")


def _advice_card(
    payload: dict[str, Any] | None, her_next_pick: int | None, over: bool
) -> dict[str, Any] | None:
    """The card, plus the two flags that decide how it is drawn.

    ``researched`` is ``source == "claude"``: a card the model wrote. Everything
    else came from the board's own ranking, and ``attempts == 0`` says no model
    call was even attempted — the budget was gone, or the advisor never got that
    far. Both are fallbacks and both are drawn as fallbacks; the distinction only
    changes the sentence.

    ``stale`` compares the card against **her next pick**, not against the pick
    on the clock: a card written two picks early is the normal case, not a stale
    one. It goes stale when she has picked and her next turn is a different
    pick — which is precisely when the loop writes a new one.
    """
    if payload is None:
        return None
    written_for = _written_for(payload)
    attempts = payload.get("attempts")
    return {
        "pick": payload.get("pick"),
        "reason": payload.get("reason"),
        "backups": [
            {"name": item.get("name"), "reason": item.get("reason")}
            for item in payload.get("backups") or []
            if isinstance(item, dict) and item.get("name")
        ],
        "watch_out": payload.get("watch_out"),
        "written_for": written_for,
        "created_at": payload.get("created_at"),
        "researched": payload.get("source") == "claude",
        "tried_to_research": bool(attempts),
        # Once the draft is over nothing is "current", so nothing is stale
        # either; and with no league there is no next pick to compare against,
        # so the card is left alone rather than called wrong on a guess.
        "stale": (
            (not over)
            and written_for is not None
            and her_next_pick is not None
            and written_for != her_next_pick
        ),
    }


def _roster(
    conn: sqlite3.Connection,
    league: LeagueContext | None,
    board: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Her team so far, by slot, with the open slots as empty rows.

    Built from the **picks**, not from ``roster_slots``: during a draft only
    ``sync_draft`` is running, so the roster table is whatever the last full
    league sync left behind and is usually hours stale. A pick she made ninety
    seconds ago has to appear here.
    """
    if league is None:
        return []

    slots = {
        slot: count
        for slot, count in league.roster_slots.items()
        if slot.upper() not in _NOT_DRAFTED and count > 0
    }
    if not slots:
        return []

    by_name = {
        board_math.normalize_name(entry["name"]): entry for entry in board if entry.get("name")
    }
    drafted: list[dict[str, Any]] = []
    for pick in store.picks_for_team(conn, league.my_team_id):
        name = pick.get("player_name")
        if not name:
            continue
        entry = by_name.get(board_math.normalize_name(str(name)), {})
        drafted.append(
            {
                "name": name,
                "position": entry.get("position"),
                "position_word": position_word(entry.get("position")),
                "pro_team": entry.get("pro_team"),
                "bye_week": entry.get("bye_week"),
                "overall_pick": pick.get("overall_pick"),
            }
        )

    # Most restrictive slot first, exactly as ``roster_needs`` does it: a
    # quarterback can only fill QB, while a running back could fill RB or the
    # flex, so spending the flex before the dedicated slots understates a need.
    remaining = dict(slots)
    order = sorted(remaining, key=lambda slot: len(board_math.slot_positions(slot)))
    placed: dict[str, list[dict[str, Any]]] = {slot: [] for slot in slots}
    bench: list[dict[str, Any]] = []
    for player in drafted:
        position = (player.get("position") or "").upper()
        for slot in order:
            if remaining[slot] > 0 and position in board_math.slot_positions(slot):
                remaining[slot] -= 1
                placed[slot].append(player)
                break
        else:
            bench.append(player)

    groups = [
        {
            "slot": slot,
            "label": slot_label(slot),
            "players": placed[slot],
            "open": remaining[slot],
        }
        for slot in sorted(slots, key=slot_sort_key)
        if slot.upper() not in {"BE", "BN", "BENCH"}
    ]
    if bench:
        # Only players, never empty rows. A starting slot with nobody in it is
        # the thing this card exists to show; seven empty bench rows are seven
        # lines of nothing pushing the board off a phone screen.
        groups.append(
            {"slot": "BE", "label": slot_label("BE"), "players": bench, "open": 0}
        )
    return groups


def _board_view(board: list[dict[str, Any]], position: str | None) -> dict[str, Any]:
    """Who is left, who just went, and the filters worth offering.

    Drafted players are struck through rather than deleted — she needs to see
    that the name she was about to say has gone — but they are shown as a short
    "just taken" list rather than interleaved, because a board thirty rows deep
    where twenty of them are crossed out is a board with nothing on it.
    """
    wanted = [position] if position else None
    available = board_math.available(board, limit=BOARD_ROWS, positions=wanted)
    gone = [
        entry
        for entry in board
        if (entry.get("drafted") or entry.get("drafted_by_team_id") is not None)
        and (position is None or (entry.get("position") or "").upper() == position)
    ]
    gone.sort(key=lambda entry: entry.get("drafted_at") or "", reverse=True)

    # Every position the board *carries*, not only the ones with players left.
    # A run on tight ends must not delete the button she is standing on.
    present = {
        (entry.get("position") or "").upper() for entry in board if entry.get("position")
    }
    filters = [
        {"code": code, "label": position_plural(code), "selected": code == position}
        for code in list(FILTER_ORDER) + sorted(present - set(FILTER_ORDER))
        if code in present
    ]
    return {
        "missing": not board,
        "available": [_board_row(entry) for entry in available],
        "gone": [_board_row(entry) for entry in gone[:GONE_ROWS]],
        "filters": filters,
        "position": position,
        "position_label": position_plural(position) if position else None,
        # Singular, for "every tight end has been taken" — the plural label
        # belongs on the button and reads as a grammatical error in a sentence.
        #
        # Looked up directly rather than through ``position_word``, which falls
        # back to the raw code. That fallback is right beside a player's name,
        # where the code is what ESPN shows her; in a sentence it produces
        # "every ZZ on the list has been taken", which is jargon arriving by
        # accident on the page whose whole rule is that there is none.
        "position_word": POSITION_WORDS.get(position) if position else None,
        "built_at": next((entry.get("built_at") for entry in board if entry.get("built_at")), None),
    }


def _board_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry.get("name"),
        "position": entry.get("position"),
        "position_word": position_word(entry.get("position")),
        "pro_team": entry.get("pro_team"),
        "bye_week": entry.get("bye_week"),
        "tier": entry.get("tier"),
        "note": entry.get("note"),
    }


def _unmatched(conn: sqlite3.Connection, teams: dict[int, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        raw = store.unmatched_picks(conn, limit=20)
    except sqlite3.Error:  # pragma: no cover - an unmigrated database
        return []
    for row in raw:
        team_id = row.get("team_id")
        rows.append(
            {
                "id": row.get("id"),
                "name": row.get("player_name") or f"player {row.get('player_id')}",
                "overall_pick": row.get("overall_pick"),
                "team": teams.get(team_id) if team_id is not None else None,
                "noticed": spell_out_age(seconds_since(row.get("noticed_at"))),
            }
        )
    return rows


def _something_is_polling(
    conn: sqlite3.Connection, settings: Settings
) -> bool:
    """Is anything actually reading ESPN right now?

    The draft loop is allowed to be absent — ``start_draft_loop`` tolerates a
    loop that will not start, because serving the page matters more than the
    loop that feeds it. So "a card is being written" must be falsifiable, or the
    page promises one forever and she waits for it.

    ``sync_draft`` writes a ``sync_runs`` row every tick, so a recent one is the
    loop's own evidence of life. No row at all means nothing has ever polled.
    """
    row = last_sync(conn, "draft")
    if row is None:
        return False
    seconds = seconds_since(row["finished_at"] or row["started_at"])
    return seconds is not None and seconds <= settings.web.draft_stale_seconds


def _staleness(
    conn: sqlite3.Connection, settings: Settings, turn: dict[str, Any]
) -> dict[str, Any] | None:
    """How far behind the draft sync is, but only while it matters.

    Before the first pick there is nothing to be behind on, and after the last
    one there never will be again. Banding the page in either state trains her
    to ignore the band on the one night it means something.
    """
    if turn.get("over") or not turn.get("started"):
        return None
    row = last_sync(conn, "draft")
    if row is None:
        return None
    seconds = seconds_since(row["finished_at"] or row["started_at"])
    if seconds is None or seconds <= settings.web.draft_stale_seconds:
        return None
    return {"seconds": int(seconds), "age": spell_out_age(seconds)}


# --- the whole page ----------------------------------------------------------


def draft_context(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    position: str | None = None,
    loop_error: str | None = None,
) -> dict[str, Any]:
    """Assemble the draft page. Never raises on a database in any state."""
    code = (position or "").strip().upper() or None

    try:
        league: LeagueContext | None = load_league_context(conn, settings)
        league_problem = None
    except LeagueUnknown as exc:
        # Not fatal, and not silent. Without a league there is no pick number
        # and no roster shape, but the board and the manual entry still work.
        league, league_problem = None, str(exc)

    board = store.load_board(conn)
    teams = _teams(conn)
    next_pick = store.next_overall_pick(conn)
    turn = _turn(league, teams, next_pick)
    her_next_pick = (turn.get("my_next_picks") or [None])[0]
    advice = _advice_card(_latest_advice(conn), her_next_pick, bool(turn.get("over")))

    # "Working on it" is derived, not stored: the advisor publishes when it is
    # done and says nothing when it starts. Her pick being inside the advisor's
    # window with no card for it is exactly the window in which one is being
    # written — and showing that beats showing an empty space or, worse, last
    # turn's recommendation dressed as this one's.
    #
    # Gated on something actually polling, because the inference is otherwise
    # unfalsifiable: with no loop running the band would promise a card forever.
    advising = bool(
        league is not None
        and board
        and not turn.get("over")
        and turn.get("picks_until_mine") is not None
        and turn["picks_until_mine"] <= settings.draft.advise_within_picks
        and (advice is None or advice["stale"])
        and _something_is_polling(conn, settings)
    )

    return {
        "turn": turn,
        "league_problem": league_problem,
        "advice": advice,
        "advising": advising,
        "roster": _roster(conn, league, board),
        "board": _board_view(board, code),
        "unmatched": _unmatched(conn, teams),
        "stale": _staleness(conn, settings, turn),
        "loop_error": loop_error,
        "teams": [{"team_id": team_id, "name": name} for team_id, name in teams.items()],
        "my_team_id": league.my_team_id if league is not None else settings.team_id,
        "position": code,
        "poll_ms": settings.web.live_poll_ms,
    }
