"""The on-the-clock advisor. It has ninety seconds and it may not come back empty.

This is the fast half of draft night. All the research already happened in
``jobs.board_build``, the day before, and this call reasons only over what that
job left behind: the board, Caroline's roster, how thin each position is getting,
the last few picks, and the notes retrieved for the leading candidates. The
``draft_advice`` job's configured tool list is empty, which is not a tuning
choice — a Claude call with web search takes 20 to 120 seconds against a
90-second pick clock, so putting one here would break draft night rather than
slow it down.

**The failure path is the feature.** Caroline is looking at a running clock, and
a card that never appears is worse than a mediocre recommendation. So:

1. the full call; if it fails or comes back unparseable,
2. one retry with a shorter prompt — fewer candidates and no retrieved notes,
   because a first attempt that ran out of time will not do better with the same
   input; and if that fails too,
3. a deterministic recommendation computed from the board alone: the
   highest-ranked undrafted player at a position she still has to fill, with a
   reason that says plainly it is the fallback.

Nothing here raises. Every path ends in an ``advice`` row and an ``advice``
event.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from hal_mary import db, memory, prompts
from hal_mary.config import Settings
from hal_mary.draft import store
from hal_mary.draft.board import (
    available,
    normalize_name,
    roster_needs,
    scarcity,
    slot_positions,
)
from hal_mary.league import LeagueContext, load_league_context

__all__ = ["ADVICE_SCHEMA", "JOB_NAME", "PROMPT_FILE", "RETRY_PROMPT_FILE", "advise"]

log = logging.getLogger(__name__)

#: Position codes in words Caroline uses. Anything not here is left as it is.
_POSITION_WORDS = {
    "QB": "quarterback",
    "RB": "running back",
    "WR": "receiver",
    "TE": "tight end",
    "K": "kicker",
    "D/ST": "defence",
}

JOB_NAME = "draft_advice"
PROMPT_FILE = "draft_advice.md"
RETRY_PROMPT_FILE = "draft_advice_short.md"

#: What the draft page renders. Kept small on purpose: one recommendation, the
#: reason, a couple of fallbacks for when he is taken first, and one warning.
ADVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pick", "reason", "backups", "watch_out"],
    "properties": {
        "pick": {
            "type": "string",
            "description": "The full name of the one player to draft, from the board.",
        },
        "reason": {
            "type": "string",
            "description": (
                "Two or three sentences, no jargon, saying why him and why now."
            ),
        },
        "backups": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "reason"],
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string", "description": "One short sentence."},
                },
            },
            "description": "Who to take instead if the first choice is gone.",
        },
        "watch_out": {
            "type": "string",
            "description": "One sentence: the risk in this pick, in plain words.",
        },
    },
}


def advise(
    conn: sqlite3.Connection,
    settings: Settings,
    runner: Any,
    bus: Any,
    *,
    next_overall_pick: int,
) -> dict[str, Any]:
    """Recommend one player for ``next_overall_pick``. Never raises, never empty.

    Returns the advice with ``source`` set to ``claude`` or ``fallback`` and
    ``attempts`` set to how many Claude calls were made, and leaves behind an
    ``advice`` row and a published ``advice`` event.
    """
    league = load_league_context(conn, settings)
    board = store.load_board(conn)
    state = _draft_state(conn, league, board, next_overall_pick)

    advice: dict[str, Any] | None = None
    attempts = 0
    for prompt_file, candidate_count, note_limit in (
        (PROMPT_FILE, settings.draft.advice_candidates, settings.draft.advice_note_limit),
        (RETRY_PROMPT_FILE, settings.draft.advice_retry_candidates, 0),
    ):
        attempts += 1
        advice = _ask(
            conn,
            settings,
            runner,
            league,
            state,
            prompt_file=prompt_file,
            candidate_count=candidate_count,
            note_limit=note_limit,
        )
        if advice is not None:
            break

    source = "claude"
    if advice is None:
        source = "fallback"
        advice = _fallback(state)

    payload = {
        **advice,
        "source": source,
        "attempts": attempts,
        "next_overall_pick": next_overall_pick,
        "picks_until_mine": state["picks_until_mine"],
        "my_next_picks": state["my_next_picks"],
        "board_built_at": state["built_at"],
    }
    payload["advice_id"] = _persist(conn, payload)
    _publish(bus, payload)
    return payload


# --- one attempt -------------------------------------------------------------


def _ask(
    conn: sqlite3.Connection,
    settings: Settings,
    runner: Any,
    league: LeagueContext,
    state: dict[str, Any],
    *,
    prompt_file: str,
    candidate_count: int,
    note_limit: int,
) -> dict[str, Any] | None:
    """One Claude call. ``None`` means "unusable", for any reason at all."""
    candidates = state["candidates"][:candidate_count]
    try:
        prompt = prompts.render_prompt(
            settings, prompt_file, _prompt_values(league, state, candidates)
        )
        context = memory.build_context(
            conn,
            settings,
            # ``query=``, not ``players=``. The players filter is an exact-ish
            # match on a normalised name; the board's names come from web
            # research and the picks come from ESPN, and the two disagree about
            # spelling often enough that the exact filter quietly returns
            # nothing. ``query=`` tokenizes and is forgiving.
            query=_candidate_query(candidates),
            note_limit=note_limit,
            extra_sections=_sections(league, state, candidates),
        )
        # Live state travels as extra_context, never glued onto the prompt: the
        # runner owns how the two are assembled, and a prompt built by string
        # concatenation is one that cannot be cached or diffed.
        result = runner.run(JOB_NAME, prompt, schema=ADVICE_SCHEMA, extra_context=context)
    except Exception:
        log.exception("draft advice call failed before it returned")
        return None

    advice = _parse(result)
    if advice is None:
        # ``ok=False`` still carries the prose the model produced. Log it before
        # falling back: without it, a bad recommendation cannot be reconstructed
        # after the draft, and neither can a schema the model kept missing.
        log.warning(
            "draft advice unusable (%s); the model said: %s",
            getattr(result, "error", None) or "no structured output",
            (getattr(result, "text", "") or "")[:2000],
        )
    return advice


def _parse(result: Any) -> dict[str, Any] | None:
    """The advice, from structured output or from the prose, or ``None``.

    A non-zero exit code is not on its own a reason to throw away valid JSON: a
    call that produced the right answer and then tripped over its own shutdown
    still produced the right answer, and re-asking costs fifteen seconds of a
    ninety-second clock.
    """
    candidates: list[Any] = []
    if isinstance(getattr(result, "structured", None), dict):
        candidates.append(result.structured)
    text = (getattr(result, "text", "") or "").strip()
    if text.startswith("{"):
        try:
            candidates.append(json.loads(text))
        except ValueError:
            pass

    for candidate in candidates:
        pick = str(candidate.get("pick") or "").strip()
        reason = str(candidate.get("reason") or "").strip()
        if not pick or not reason:
            continue
        return {
            "pick": pick,
            "reason": reason,
            "backups": _backups(candidate.get("backups")),
            "watch_out": str(candidate.get("watch_out") or "").strip(),
        }
    return None


def _backups(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    backups = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            backups.append({"name": name, "reason": str(entry.get("reason") or "").strip()})
    return backups


# --- the state every attempt reasons over ------------------------------------


def _draft_state(
    conn: sqlite3.Connection,
    league: LeagueContext,
    board: list[dict[str, Any]],
    next_overall_pick: int,
) -> dict[str, Any]:
    roster = _my_roster(conn, league, board)
    needs = roster_needs(roster, league.roster_slots)
    upcoming = league.upcoming_picks(next_overall_pick)
    return {
        "next_overall_pick": next_overall_pick,
        "round_num": (next_overall_pick - 1) // league.team_count + 1,
        # Empty upcoming picks is the only end-of-draft signal the board
        # arithmetic gives; picks_until_mine would happily count down forever.
        "draft_over": not upcoming,
        "my_next_picks": upcoming[:2],
        "picks_until_mine": league.picks_until_mine(next_overall_pick) if upcoming else None,
        "roster": roster,
        "needs": needs,
        "open_positions": _open_positions(needs),
        "candidates": available(board, limit=max(len(board), 1)),
        "scarcity": scarcity(board),
        "recent": store.recent_picks(conn, limit=8),
        "built_at": board[0].get("built_at") if board else None,
    }


def _my_roster(
    conn: sqlite3.Connection, league: LeagueContext, board: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Who Caroline has already taken, with positions.

    Built from the board first, because a board row carries the position and a
    draft pick does not. Picks she made for players the board never carried are
    then looked up in the synced ``players`` table, so a reach for someone
    unranked still fills a roster slot.
    """
    roster = [
        {"name": entry["name"], "position": entry.get("position")}
        for entry in board
        if entry.get("drafted_by_team_id") == league.my_team_id
    ]
    seen = {normalize_name(player["name"]) for player in roster}

    for pick in store.picks_for_team(conn, league.my_team_id):
        name = pick.get("player_name")
        if not name or normalize_name(name) in seen:
            continue
        seen.add(normalize_name(name))
        row = conn.execute(
            "SELECT position FROM players WHERE player_id = ?", (pick.get("player_id"),)
        ).fetchone()
        roster.append({"name": name, "position": row["position"] if row else None})
    return roster


def _open_positions(needs: dict[str, int]) -> list[str]:
    """Positions that would fill at least one still-open starting slot."""
    positions: set[str] = set()
    for slot, open_count in needs.items():
        if open_count > 0:
            positions |= slot_positions(slot)
    return sorted(positions)


def _candidate_query(candidates: list[dict[str, Any]]) -> str:
    return " ".join(str(entry.get("name") or "") for entry in candidates).strip()


# --- prompt and context ------------------------------------------------------


def _prompt_values(
    league: LeagueContext, state: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "team_count": league.team_count,
        "scoring_summary": league.scoring_summary,
        "rounds": league.rounds,
        "round_num": state["round_num"],
        "next_overall_pick": state["next_overall_pick"],
        "my_next_picks": _join(state["my_next_picks"]),
        "candidate_count": len(candidates),
    }


def _sections(
    league: LeagueContext, state: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, str]:
    """The live state, as Markdown sections for Task 4's context block."""
    return {
        "Where the draft is right now": _where(league, state),
        "Caroline's team so far": _team(state),
        "Best players still available": _available(candidates),
        "How thin each position is getting": _scarcity(state["scarcity"]),
        "The last few picks": _recent(state["recent"]),
    }


def _where(league: LeagueContext, state: dict[str, Any]) -> str:
    lines = [
        (
            f"- Pick {state['next_overall_pick']} of {league.total_picks} is on the clock "
            f"(round {state['round_num']} of {league.rounds})."
        ),
        f"- **Caroline's next two picks are {_join(state['my_next_picks'])}.**",
    ]
    if state["picks_until_mine"] == 0:
        lines.append("- **She is on the clock now.** She has about 90 seconds.")
    elif state["picks_until_mine"] is not None:
        lines.append(
            f"- {state['picks_until_mine']} other team(s) pick before she does."
        )
    if len(state["my_next_picks"]) >= 2 and state["my_next_picks"][1] - state["my_next_picks"][0] == 1:
        lines.append(
            "- Those two picks are **back to back**, so treat them as one decision: "
            "the pair should cover two different positions, not the same one twice."
        )
    return "\n".join(lines)


def _team(state: dict[str, Any]) -> str:
    if state["roster"]:
        held = "\n".join(
            f"  - {player['name']} ({player['position'] or 'position unknown'})"
            for player in state["roster"]
        )
    else:
        held = "  - Nobody yet. This is her first pick."
    open_slots = [
        f"  - {slot} x{count}{_slot_gloss(slot)}"
        for slot, count in state["needs"].items()
        if count > 0
    ]
    slots = "\n".join(open_slots) or "  - None. Every starting spot is filled."
    return (
        f"Players she has already drafted:\n{held}\n\n"
        f"Starting slots still to fill:\n{slots}\n\n"
        "Bench spots are not listed: they are not a need."
    )


def _slot_gloss(slot: str) -> str:
    """Explain a multi-position slot **by the name this league gives it**.

    ESPN names this league's flex slot ``RB/WR/TE``, not ``FLEX`` — confirmed
    from the live payload. Hardcoding an explanation of "a FLEX slot" would
    define a term that appears nowhere on Caroline's screen, which is worse than
    no gloss at all. So the words come from the slot's own positions.
    """
    positions = slot_positions(slot)
    if len(positions) < 2:
        return ""
    words = [_POSITION_WORDS.get(position, position) for position in sorted(positions)]
    listed = ", ".join(f"a {word}" for word in words[:-1]) + f" or a {words[-1]}"
    return f" — one extra starter who can be {listed}"


def _available(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "The board is empty. There are no researched players to choose from."
    lines = ["Best first. Tier 1 is best; players inside one tier are interchangeable.", ""]
    for entry in candidates:
        bye = entry.get("bye_week")
        bits = [
            (
                f"- **{entry['name']}** ({entry.get('position') or '?'}"
                f"{', ' + entry['pro_team'] if entry.get('pro_team') else ''})"
            ),
            f"tier {entry.get('tier')}",
            f"ranked {entry.get('rank')} overall",
        ]
        if bye:
            bits.append(f"bye week {bye}")
        line = " — ".join(bits)
        if entry.get("note"):
            line += f". {entry['note']}"
        lines.append(line)
    return "\n".join(lines)


def _scarcity(counts: dict[str, dict[str, int | None]]) -> str:
    lines = []
    for position, detail in sorted(counts.items()):
        best = detail.get("best_tier")
        if best is None:
            # A position with no tiered rows left reports a count that means
            # nothing. Say so rather than presenting it as scarcity.
            lines.append(
                f"- {position}: nobody left with a researched grade, so this is unknown."
            )
            continue
        lines.append(
            f"- {position}: the best left is tier {best}, and {detail['count']} player(s) "
            "are at that level or one below it."
        )
    return "\n".join(lines) or "Nothing left on the board."


def _recent(picks: list[dict[str, Any]]) -> str:
    if not picks:
        return "No picks have been made yet."
    lines = []
    for pick in picks:
        # A pick whose name ESPN could not supply is shown by its id rather than
        # left blank: "player 4262921 is gone" is still information.
        who = pick.get("player_name") or f"player {pick.get('player_id')}"
        team = pick.get("team_id")
        lines.append(
            f"- Pick {pick.get('overall_pick')}: {who} — "
            f"{'team ' + str(team) if team is not None else 'entered by hand'}"
        )
    return "\n".join(lines)


def _join(numbers: list[int]) -> str:
    if not numbers:
        return "none left — the draft is over"
    if len(numbers) == 1:
        return str(numbers[0])
    return f"{numbers[0]} and {numbers[1]}"


# --- the deterministic fallback ----------------------------------------------


def _fallback(state: dict[str, Any]) -> dict[str, Any]:
    """The board's own answer, for when Claude could not give one.

    The highest-ranked undrafted player at a position Caroline still has to
    start. It says plainly that it is the fallback, because advice she cannot
    tell apart from a researched recommendation is advice she cannot weigh.
    """
    needed = state["open_positions"]
    ranked = [
        entry
        for entry in state["candidates"]
        if not needed or (entry.get("position") or "").upper() in needed
    ]
    # Every starting slot full is a real state late in a draft: fall back to the
    # best player left regardless of position, which is what a bench pick is.
    pool = ranked or state["candidates"]

    if not pool:
        slot = needed[0] if needed else "any position"
        return {
            "pick": f"the best available {slot}",
            "reason": (
                "hal-mary could not reach its advisor and has no researched board to fall "
                f"back on, so it cannot name a player. Take the best {slot} in ESPN's own "
                "list — ESPN sorts it best-first — and check back next pick."
            ),
            "backups": [],
            "watch_out": (
                "This is not a recommendation, it is a stand-in. Trust your own eyes on "
                "this one pick."
            ),
        }

    best = pool[0]
    position = best.get("position") or "his position"
    reason = (
        f"hal-mary could not get a full answer in time, so this is the board's own: "
        f"{best['name']} is the highest-ranked player left "
        + (
            f"at a position you still have to fill ({position}). "
            if ranked
            else "on the whole board, and every starting spot is already filled. "
        )
        + f"He is graded tier {best.get('tier')}, ranked {best.get('rank')} overall."
    )
    if best.get("note"):
        reason += f" {best['note']}"
    return {
        "pick": best["name"],
        "reason": reason,
        "backups": [
            {
                "name": entry["name"],
                "reason": f"Also tier {entry.get('tier')}, ranked {entry.get('rank')} overall.",
            }
            for entry in pool[1:3]
        ],
        "watch_out": (
            "This came from the ranking list alone, with no thought about who else is "
            "on your team or who has been taken in the last few minutes. Sanity-check it "
            "against what you can see in ESPN."
        ),
    }


# --- persistence -------------------------------------------------------------


def _persist(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    headline = f"Pick {payload['next_overall_pick']}: take {payload['pick']}"
    cur = conn.execute(
        """
        INSERT INTO advice (created_at, kind, headline, body, payload_json, source_job)
        VALUES (?, 'draft', ?, ?, ?, ?)
        """,
        (db.utc_now(), headline, payload["reason"], json.dumps(payload), JOB_NAME),
    )
    conn.commit()
    advice_id = cur.lastrowid
    if advice_id is None:  # pragma: no cover - sqlite always reports a rowid
        raise RuntimeError("sqlite did not report a row id for the new advice row")
    return advice_id


def _publish(bus: Any, payload: dict[str, Any]) -> None:
    """Publishing must never be what breaks the advice.

    The bus itself swallows a dead subscriber, but a page that is not listening
    is no reason for the advisor to fail, so this is belt and braces.
    """
    try:
        bus.publish("advice", dict(payload))
    except Exception:  # pragma: no cover - the bus does not raise
        log.exception("could not publish the advice event")
