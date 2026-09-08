"""The pre-draft board build: the slow half of draft night, run the day before.

Draft night splits in two, and the split is the single most important design
decision in this application. A Claude call with web search takes 20 to 120
seconds; the ESPN pick clock is 90. So *all* the research happens here, before
the draft, and writes a ranked, tiered board to the database. The advisor then
runs on the clock with web tools off, reasoning only over what this job left
behind. Putting a web-enabled call on the pick-clock path is a defect, not a
performance note.

Two failure modes are designed for explicitly:

* **A failed build must leave the previous board untouched.** A board from
  yesterday is worth a great deal; an empty board is worth nothing, because the
  advisor has no candidates and the deterministic fallback has nothing to fall
  back to. The replace happens in one transaction and only after a result has
  been parsed and found to contain players.
* **A board row with no tier is nearly useless.** ``scarcity`` reports
  ``best_tier: None`` for a position with no tiered rows and a count that then
  means nothing, and tiers are what make "who do I take" answerable at all. So
  every row written here is tiered, filling a gap by carrying the previous tier
  forward rather than inventing a new one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from hal_mary import db, memory, prompts
from hal_mary.config import Settings
from hal_mary.draft import store
from hal_mary.draft.board import normalize_name
from hal_mary.jobs.registry import JobFailed, register
from hal_mary.league import LeagueContext, load_league_context
from hal_mary.recency import recency_block

# Slot wording lives in exactly one place and this is a reader of it, the same
# way ``hal_mary.chat`` is. A prompt handed bare slot codes writes notes in bare
# slot codes, and "RB/WR/TE" is not a phrase Caroline can act on.
from hal_mary.web.positions import SLOT_LABELS, slot_sort_key

__all__ = ["BOARD_SCHEMA", "JOB_NAME", "PROMPT_FILE", "build_board", "run"]

log = logging.getLogger(__name__)

JOB_NAME = "board_build"
PROMPT_FILE = "board_build.md"

#: Synthetic board ids start well below this. ESPN pre-populates every unmade
#: pick with ``playerId: -1``, so a synthetic id anywhere near -1 would let an
#: unmade pick match a real board row and mark a player gone before the draft
#: even starts. Task 12 filters those at the client layer; this is the second
#: lock on the same door.
SYNTHETIC_ID_BASE = -1000

#: Shorter than this and a note is a label, not a fact worth retrieving later.
MIN_NOTE_CHARS = 12

#: What the model must return. ``structured_output`` is an object, so the list
#: is wrapped rather than being the top level.
BOARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["players"],
    "properties": {
        "players": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "position", "tier", "rank", "note"],
                "properties": {
                    "name": {"type": "string", "description": "Full name, as ESPN spells it."},
                    "position": {
                        "type": "string",
                        "enum": ["QB", "RB", "WR", "TE", "K", "D/ST"],
                    },
                    "pro_team": {
                        "type": "string",
                        "description": "NFL team abbreviation, e.g. CIN.",
                    },
                    "tier": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1 is best. Players in one tier are interchangeable.",
                    },
                    "rank": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Overall rank for this league, 1 is best.",
                    },
                    "bye_week": {
                        "type": ["integer", "null"],
                        "description": "The week his real team does not play.",
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "One sentence a beginner understands: why he is here, "
                            "and what the risk is."
                        ),
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Where this was found. One link.",
                    },
                },
            },
        }
    },
}


@register(
    JOB_NAME,
    phases=("pre_draft",),
    summary="Research and rebuild the ranked draft board.",
)
def run(
    conn: sqlite3.Connection, settings: Settings, runner: Any, client: Any = None
) -> str:
    """The registry entry point: build the board, or say why not.

    ``client`` is unused — the board is built from research and from what is
    already in the database, and the pre-draft ESPN state it needs arrives
    through ``hal-mary sync``. It is in the signature because every job has the
    same one, which is what lets the scheduler and the CLI treat them alike.

    The ``job_runs`` row is opened and closed by
    :func:`hal_mary.jobs.registry.run_job`, not here. There is exactly one place
    a run is recorded, so a job can never be reported as having started twice.
    """
    outcome = build_board(conn, settings, runner)
    if not outcome["ok"]:
        raise JobFailed(outcome["error"] or "the board build produced nothing usable")
    return outcome["summary"]


def build_board(conn: sqlite3.Connection, settings: Settings, runner: Any) -> dict[str, Any]:
    """Research the board and replace it. Returns a summary dict; never raises.

    Never raising is the point: this runs on a scheduler, and a job that throws
    on a bad night is a job whose failure is discovered by its absence. Every
    outcome comes back as a dict, and :func:`run` turns a failed one into the
    :class:`~hal_mary.jobs.registry.JobFailed` the registry records.
    """
    try:
        return _build(conn, settings, runner)
    except Exception as exc:
        log.exception("board build failed")
        return {"ok": False, "players": 0, "notes": 0, "error": str(exc)}


def _build(conn: sqlite3.Connection, settings: Settings, runner: Any) -> dict[str, Any]:
    league = load_league_context(conn, settings)
    draft_config = settings.draft

    prompt = prompts.render_prompt(settings, PROMPT_FILE, _prompt_values(league, draft_config, settings))
    context = memory.build_context(
        conn,
        settings,
        topics=["injury", "news", "draft"],
        note_limit=draft_config.research_note_limit,
    )

    result = runner.run(JOB_NAME, prompt, schema=BOARD_SCHEMA, extra_context=context)

    payload = _payload(result)
    if payload is None:
        # ``ok=False`` still carries the prose the model produced. Log it: a
        # build that half-worked is reconstructable from the transcript, and the
        # error alone never says which sources it had already found.
        log.warning("board build produced no usable board; model said: %s", (result.text or "")[:2000])
        return {
            "ok": False,
            "players": 0,
            "notes": 0,
            "error": result.error or "the research call returned no usable player list",
        }

    rows = _normalize(payload, conn)
    if not rows:
        return {
            "ok": False,
            "players": 0,
            "notes": 0,
            "error": "the research call returned no players; the previous board is kept",
        }

    built_at = db.utc_now()
    notes = _notes_for(rows)
    with db.transaction(conn):
        store.replace_board(conn, rows, built_at)
        memory.write_notes(conn, notes)

    summary = f"{len(rows)} players on the board, {len(notes)} notes"
    log.info("board build: %s", summary)
    return {
        "ok": True,
        "players": len(rows),
        "notes": len(notes),
        "error": None,
        "summary": summary,
        "built_at": built_at,
        "cost_usd": result.cost_usd,
    }


# --- prompt ------------------------------------------------------------------


def _prompt_values(
    league: LeagueContext, draft_config: Any, settings: Settings
) -> dict[str, Any]:
    """The league's real facts, in the words the prompt file expects.

    Every one of these is a fact a model would otherwise assume wrongly. Six
    teams rather than twelve, a point per catch rather than none, and a pick at
    the turn rather than in the middle each change which player is the right
    one, and all three are true at once here.
    """
    picks = league.upcoming_picks(1)
    first_two = picks[:2]
    return {
        "season": league.season or "this",
        "league_name": league.name or "Caroline's league",
        "team_count": league.team_count,
        "scoring_summary": league.scoring_summary,
        "scoring_type": league.scoring_type or "unknown",
        "draft_type": (league.draft_type or "snake").lower(),
        "roster_slots": _slot_lines(league),
        "recency": recency_block(settings),
        "league_shape": _shape_lines(league),
        "playoff_summary": league.playoff_summary,
        "max_source_players": int(draft_config.board_size * draft_config.max_source_share),
        "rounds": league.rounds,
        "total_picks": league.total_picks,
        "my_draft_slot": league.my_draft_slot,
        "first_two_picks": " and ".join(str(pick) for pick in first_two) or "unknown",
        "all_my_picks": ", ".join(str(pick) for pick in picks) or "unknown",
        "draft_date": league.draft_date or "not set in ESPN, so treat it as unknown",
        "board_size": draft_config.board_size,
    }


def _slot_lines(league: LeagueContext) -> str:
    """The roster, named the way Caroline's own screen names it.

    The code stays beside the words rather than replacing them — ESPN shows the
    code, so it is worth recognising — but it is never the only label, which is
    the rule ``web/positions.py`` exists to hold.
    """
    ordered = sorted(league.roster_slots.items(), key=lambda item: slot_sort_key(item[0]))
    return "\n".join(
        f"  - {SLOT_LABELS.get(slot, slot)} (ESPN calls this slot `{slot}`): {count}"
        for slot, count in ordered
    )


def _shape_lines(league: LeagueContext) -> str:
    """Replacement level, worked out here rather than left to the model.

    This is the arithmetic no published ranking can carry, because every
    published ranking is written for some other number of teams. How many
    players are drafted at all, and how many of each kind actually start in any
    given week, is what decides whether a position is scarce — and it is
    bookkeeping, which is Python's half of this application.

    Bench and injured-reserve slots are excluded from the weekly counts on
    purpose: a bench player scores nothing, so the number that sets replacement
    level is the number of *starting* slots across the league.
    """
    lines = [
        (
            f"- {league.total_picks} players are drafted in total "
            f"({league.team_count} teams x {league.rounds} rounds). Every other player in "
            "the NFL is unowned when the draft ends, and can be picked up free at any "
            "point in the season."
        ),
    ]
    starters = sorted(league.starting_slots.items(), key=lambda item: slot_sort_key(item[0]))
    for slot, count in starters:
        league_wide = count * league.team_count
        lines.append(
            f"- {SLOT_LABELS.get(slot, slot)}: {count} per team, so {league_wide} start "
            "across the whole league in any given week."
        )
    return "\n".join(lines)


# --- parsing and normalising -------------------------------------------------


def _payload(result: Any) -> list[dict[str, Any]] | None:
    """The player list, from structured output or from the prose as a fallback.

    A call that returned the right JSON with a non-zero exit code still returned
    the right JSON, and a board is expensive enough to be worth rescuing.
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
        players = candidate.get("players")
        if isinstance(players, list) and players:
            return [row for row in players if isinstance(row, dict)]
    return None


def _normalize(players: list[dict[str, Any]], conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Clean the model's list into board rows: ranked, tiered, uniquely named."""
    known = _known_player_ids(conn)

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in players:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        key = normalize_name(name)
        if key in seen:
            # Two rows for one player would make every pick for him ambiguous,
            # and ``apply_picks`` refuses to guess between them — so he would
            # never be marked gone.
            continue
        seen.add(key)
        cleaned.append(
            {
                "name": name,
                "position": (str(raw.get("position") or "").strip().upper() or None),
                "pro_team": (str(raw.get("pro_team") or "").strip().upper() or None),
                "tier": _positive_int(raw.get("tier")),
                "rank": _positive_int(raw.get("rank")),
                "bye_week": _positive_int(raw.get("bye_week")),
                "note": str(raw.get("note") or "").strip(),
                "source_url": str(raw.get("source_url") or "").strip() or None,
            }
        )

    # Rank order is the board's order. A row with no rank sorts behind the
    # ranked ones rather than to the top, where it would look like a steal.
    cleaned.sort(key=lambda row: (row["rank"] is None, row["rank"] if row["rank"] else 0))

    last_tier = 1
    for index, row in enumerate(cleaned, start=1):
        if row["rank"] is None:
            row["rank"] = index
        if row["tier"] is None:
            row["tier"] = last_tier
        else:
            last_tier = row["tier"]
        row["player_id"] = known.get(normalize_name(row["name"]), SYNTHETIC_ID_BASE - index)
    return cleaned


def _known_player_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Normalized name -> ESPN player id, for names that are unambiguous.

    Worth doing because id matching is the only stage of ``apply_picks`` that
    still works when ESPN's player-name map is briefly unavailable and a pick
    arrives with no name at all. A name that two players share is left out: a
    wrong id marks the wrong man gone.
    """
    counts: dict[str, list[int]] = {}
    for row in conn.execute("SELECT player_id, name FROM players WHERE name IS NOT NULL"):
        counts.setdefault(normalize_name(row["name"]), []).append(int(row["player_id"]))
    return {key: ids[0] for key, ids in counts.items() if len(ids) == 1}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _notes_for(rows: list[dict[str, Any]]) -> list[memory.Note]:
    """One note per player whose note says something.

    ``player_name`` is the board's own spelling, exactly. Task 4 normalises both
    sides of a name comparison, but it cannot repair a name that was never
    written — a note filed under a spelling the board does not use is a note the
    advisor will never retrieve.
    """
    notes: list[memory.Note] = []
    for row in rows:
        text = row["note"]
        if len(text) < MIN_NOTE_CHARS:
            continue
        notes.append(
            memory.Note(
                text=f"{row['name']} ({row['position'] or '?'}, tier {row['tier']}): {text}",
                source_job=JOB_NAME,
                topic="draft-board",
                player_name=row["name"],
                team_abbr=row["pro_team"],
                source_url=row["source_url"],
            )
        )
    return notes
