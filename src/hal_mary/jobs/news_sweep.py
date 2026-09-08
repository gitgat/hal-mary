"""The news sweep: what changed about her players, and about the best players
she could still claim.

This is the job the other three read from. It runs first in the week — Wednesday,
once the injury reports start, and again Saturday, once the weekend's news has
landed — and it writes nothing but ``notes``. That is deliberate: the waiver scan
and the lineup check both retrieve from the note store, so one expensive sweep of
the web feeds every decision made off it for the rest of the week, and the jobs
that run against a clock do not each pay for their own research.

**Every note gets a shelf life.** "He practised in full on Wednesday" is worth a
great deal on Sunday and is actively misleading three weeks later, and a model
reading a note has no way to tell a stale one from a fresh one. So an
``expires_at`` is stamped on everything written here: from the model's own
estimate when it gives one, and from ``research.note_shelf_life_days`` when it
does not.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from hal_mary import memory, prompts
from hal_mary.config import Settings
from hal_mary.jobs import season
from hal_mary.jobs.registry import JobFailed, register
from hal_mary.league import LeagueUnknown, load_league_context

__all__ = ["JOB_NAME", "NEWS_SCHEMA", "PROMPT_FILE", "run"]

log = logging.getLogger(__name__)

JOB_NAME = "news_sweep"
PROMPT_FILE = "news_sweep.md"

#: Shorter than this and a note is a label, not a fact worth retrieving later.
MIN_NOTE_CHARS = 12

NEWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes"],
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence: the most important thing that changed.",
        },
        "notes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["player_name", "topic", "text", "source_url"],
                "properties": {
                    "player_name": {
                        "type": "string",
                        "description": "Full name, spelled as ESPN spells it.",
                    },
                    "team_abbr": {"type": "string", "description": "NFL team, e.g. CIN."},
                    "topic": {
                        "type": "string",
                        "enum": ["injury", "role", "depth-chart", "news", "matchup"],
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "One or two sentences a beginner understands, saying what "
                            "changed and what it means for whether he should be started."
                        ),
                    },
                    "source_url": {"type": "string", "description": "Where this was found."},
                    "days_valid": {
                        "type": ["integer", "null"],
                        "description": (
                            "How many days this stays true. A weekly injury report is "
                            "about 7; a season-ending injury is 200."
                        ),
                    },
                },
            },
        },
    },
}


@register(
    JOB_NAME,
    phases=("in_season",),
    summary="Injuries, role changes and depth-chart moves, for her players and the wire.",
)
def run(
    conn: sqlite3.Connection, settings: Settings, runner: Any, client: Any = None
) -> str:
    """Sweep the news. Raises :class:`JobFailed` when nothing usable came back."""
    stale = season.refresh_from_espn(conn, client)
    roster = season.my_roster(conn, settings)
    if not roster:
        raise JobFailed(
            "there is no roster in the database yet, so there is nobody to read the news "
            "about. Run `hal-mary sync` first."
        )

    free_agents = _free_agents(settings, client)
    prompt = prompts.render_prompt(
        settings,
        PROMPT_FILE,
        _prompt_values(conn, settings, roster, free_agents, stale, client),
    )
    context = memory.build_context(
        conn,
        settings,
        players=season.player_names(roster),
        topics=["injury", "news", "role"],
        note_limit=settings.research.note_limit,
    )

    result = runner.run(JOB_NAME, prompt, schema=NEWS_SCHEMA, extra_context=context)
    payload = season.structured_payload(result)
    notes = _notes_from(payload, settings) if payload else []
    if not notes:
        log.warning(
            "news sweep produced no usable notes; the model said: %s",
            (getattr(result, "text", "") or "")[:2000],
        )
        raise JobFailed(
            getattr(result, "error", None) or "the news sweep returned nothing usable"
        )

    memory.write_notes(conn, notes)
    players = len({note.player_name for note in notes if note.player_name})
    return f"{len(notes)} notes on {players} players"


def _free_agents(settings: Settings, client: Any) -> list[dict[str, Any]]:
    """The wire, or an empty list. A sweep of her own roster is still worth having."""
    if client is None:
        return []
    try:
        return list(client.free_agents(size=settings.research.free_agent_size))
    except Exception:
        log.warning("could not read the free agent list; sweeping her roster only", exc_info=True)
        return []


def _notes_from(payload: dict[str, Any], settings: Settings) -> list[memory.Note]:
    notes: list[memory.Note] = []
    for raw in payload.get("notes") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        name = str(raw.get("player_name") or "").strip()
        if len(text) < MIN_NOTE_CHARS or not name:
            continue
        notes.append(
            memory.Note(
                text=text,
                source_job=JOB_NAME,
                topic=(str(raw.get("topic") or "news").strip() or "news"),
                player_name=name,
                team_abbr=(str(raw.get("team_abbr") or "").strip().upper() or None),
                source_url=(str(raw.get("source_url") or "").strip() or None),
                expires_at=_expiry(raw.get("days_valid"), settings),
            )
        )
    return notes


def _expiry(days_valid: Any, settings: Settings) -> str:
    """When this note stops being trustworthy.

    Always a value, never ``None``. A note written here is a snapshot of one
    week's news, and one with no expiry lingers in prompts long after it stopped
    being true — which is worse than never having written it, because Claude
    reads it as current.
    """
    try:
        days = int(days_valid)
    except (TypeError, ValueError):
        days = settings.research.note_shelf_life_days
    if days <= 0:
        days = settings.research.note_shelf_life_days
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")


def _prompt_values(
    conn: sqlite3.Connection,
    settings: Settings,
    roster: list[dict[str, Any]],
    free_agents: list[dict[str, Any]],
    stale: str | None,
    client: Any,
) -> dict[str, Any]:
    week = season.current_week(conn, settings, client)
    try:
        league = load_league_context(conn, settings)
        scoring = league.scoring_summary
        team_count: Any = league.team_count
    except LeagueUnknown:
        scoring = "Every catch is worth 1 point on its own (full PPR)."
        team_count = "six"
    return {
        "week": week if week is not None else "unknown — work it out from the NFL schedule",
        "team_count": team_count,
        "scoring_summary": scoring,
        "roster": season.roster_lines(roster, week),
        "free_agents": (
            season.free_agent_lines(free_agents, settings.research.free_agent_shortlist)
            or "  - hal-mary could not read the free agent list this time."
        ),
        "freshness": stale or "This roster was read from ESPN moments ago.",
    }
