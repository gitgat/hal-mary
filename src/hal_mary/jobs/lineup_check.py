"""Start and sit, every week — and the bye-week alarm that is the point of it.

A player on a bye week does not play. He scores **zero**. Nobody has ever meant
to start one, everybody who is new to fantasy football does it at least once,
and it is completely preventable from information hal-mary already has written
down. That is why this job exists, and it is why the bye check here is
*arithmetic*, not something asked of a model:

* The alarm is computed in Python from the roster and the week, and it goes into
  an ``advice`` row of its own with the player's name in the headline. Not a
  bullet three quarters of the way down a lineup card she is skimming on a
  phone.
* **The alarm is written even when the Claude call fails.** Every other part of
  this job depends on live research; the bye does not. A research call that
  timed out is not a reason to keep quiet about the one thing that costs a whole
  week's points.
* **Either source raising the flag is enough.** The board's bye week and the one
  the model reports may disagree; when they do, hal-mary warns. Checking a false
  alarm costs her ten seconds. Missing a real one costs the week.

It runs Sunday morning, and again Thursday and Monday, because ESPN locks each
player at his own kickoff rather than at one weekly deadline — a Thursday player
left in the lineup on a bye is already lost by Sunday morning.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from hal_mary import memory, prompts
from hal_mary.config import Settings
from hal_mary.draft.board import normalize_name
from hal_mary.jobs import season
from hal_mary.jobs.registry import JobFailed, register
from hal_mary.league import LeagueUnknown, load_league_context

__all__ = ["JOB_NAME", "LINEUP_SCHEMA", "PROMPT_FILE", "run"]

log = logging.getLogger(__name__)

JOB_NAME = "lineup_check"
PROMPT_FILE = "lineup_check.md"

#: How many stored notes go into the prompt. The lineup question is about
#: fifteen named players, so retrieval is narrow and this can be generous.
NOTE_LIMIT = 30

LINEUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["week", "starters", "bench", "headline"],
    "properties": {
        "week": {"type": "integer", "description": "The NFL week this lineup is for."},
        "headline": {
            "type": "string",
            "description": "One sentence: what she should change, or that nothing needs changing.",
        },
        "starters": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slot", "player", "reason"],
                "properties": {
                    "slot": {
                        "type": "string",
                        "description": "The ESPN roster slot, spelled as ESPN spells it.",
                    },
                    "player": {"type": "string"},
                    "position": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "One sentence a beginner understands.",
                    },
                    "bye_week": {
                        "type": ["integer", "null"],
                        "description": "The week his real team does not play.",
                    },
                    "source_url": {"type": "string"},
                },
            },
        },
        "bench": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["player", "reason"],
                "properties": {
                    "player": {"type": "string"},
                    "position": {"type": "string"},
                    "reason": {"type": "string"},
                    "bye_week": {"type": ["integer", "null"]},
                    "source_url": {"type": "string"},
                },
            },
        },
        "notes": {
            "type": "array",
            "description": "Facts worth remembering for next week.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {
                    "player_name": {"type": "string"},
                    "text": {"type": "string"},
                    "source_url": {"type": "string"},
                },
            },
        },
    },
}


@register(
    JOB_NAME,
    phases=("in_season",),
    summary="Who to start and who to sit this week, and who is on a bye.",
)
def run(
    conn: sqlite3.Connection, settings: Settings, runner: Any, client: Any = None
) -> str:
    """Check the lineup. Raises :class:`JobFailed` when the research call fails —
    *after* the bye alarm has been written, because that part never depended on it.
    """
    stale = season.refresh_from_espn(conn, client)
    roster = season.my_roster(conn, settings)
    if not roster:
        raise JobFailed(
            "there is no roster in the database yet, so there is no lineup to check. "
            "Run `hal-mary sync` first."
        )

    week = season.current_week(conn, client)
    prompt = prompts.render_prompt(
        settings, PROMPT_FILE, _prompt_values(conn, settings, roster, week, stale)
    )
    context = memory.build_context(
        conn,
        settings,
        players=season.player_names(roster),
        topics=["injury", "news", "lineup"],
        note_limit=NOTE_LIMIT,
    )

    result = runner.run(JOB_NAME, prompt, schema=LINEUP_SCHEMA, extra_context=context)
    payload = season.structured_payload(result)

    # The week the alarm is measured against: ESPN's if we have it, otherwise
    # the one the model established from the live schedule. Never guessed from
    # the calendar — a wrong week flags the wrong players, and a bye warning she
    # learns to disbelieve is worse than none at all.
    if week is None and payload is not None:
        week = _positive_int(payload.get("week"))

    alarms = _bye_alarms(conn, roster, payload, week)

    if payload is None or not payload.get("starters"):
        # The alarm still goes out. It is arithmetic over a roster and a
        # calendar, it never needed the model, and a timed-out research call is
        # no reason to keep quiet about a starter who will score zero.
        if alarms:
            _write_bye_alarm(conn, alarms, week)
        log.warning(
            "lineup check produced no usable lineup; the model said: %s",
            (getattr(result, "text", "") or "")[:2000],
        )
        raise JobFailed(
            getattr(result, "error", None)
            or "the lineup research call returned nothing usable"
        )

    _write_lineup(conn, payload, week, alarms)
    _write_notes(conn, payload)

    return _summary(payload, alarms, week)


# --- the bye-week alarm ------------------------------------------------------


def _bye_alarms(
    conn: sqlite3.Connection,
    roster: list[dict[str, Any]],
    payload: dict[str, Any] | None,
    week: int | None,
) -> list[dict[str, Any]]:
    """Every player in her lineup whose real team does not play this week.

    Two sources, and either one is enough:

    * ``board.bye_week``, researched before the draft and joined by name;
    * ``bye_week`` on the entry the model just returned, which is the fresher of
      the two because it was looked up on the web minutes ago.

    They can disagree. When they do, the flag is raised anyway and the alarm says
    where it came from. The asymmetry is deliberate: a false alarm costs her the
    ten seconds it takes to look at ESPN, and a missed one costs every point that
    player would have scored.
    """
    if not week:
        return []

    from_model = _model_byes(payload)
    alarms = []
    for entry in roster:
        if not entry["starting"]:
            # A bye on the bench is what a bench is for. Saying so would train
            # her to ignore the warning that matters.
            continue
        key = normalize_name(entry["name"])
        sources = []
        if entry.get("bye_week") and int(entry["bye_week"]) == week:
            sources.append("hal-mary's own notes on him")
        if from_model.get(key) == week:
            sources.append("this morning's check of the NFL schedule")
        if sources:
            alarms.append({"player": entry["name"], "slot": entry["slot"], "sources": sources})
    return alarms


def _model_byes(payload: dict[str, Any] | None) -> dict[str, int]:
    byes: dict[str, int] = {}
    if not isinstance(payload, dict):
        return byes
    for group in ("starters", "bench"):
        for entry in payload.get(group) or []:
            if not isinstance(entry, dict):
                continue
            week = _positive_int(entry.get("bye_week"))
            name = str(entry.get("player") or "").strip()
            if week and name:
                byes[normalize_name(name)] = week
    return byes


def _write_bye_alarm(
    conn: sqlite3.Connection, alarms: list[dict[str, Any]], week: int | None
) -> None:
    """One advice row saying exactly who to take out, and what it costs not to."""
    names = [alarm["player"] for alarm in alarms]
    if len(names) == 1:
        headline = f"Week {week}: take {names[0]} out of your lineup — he is on a bye"
    else:
        listed = ", ".join(names[:-1]) + f" and {names[-1]}"
        headline = f"Week {week}: take {listed} out of your lineup — they are on a bye"

    lines = [
        (
            "A bye week is a week a real NFL team does not play at all. A player on a "
            "bye plays no game, so he scores **zero** — not a low score, nothing. "
            "Leaving one in your lineup is the most common way to lose a week you "
            "would otherwise have won."
        ),
        "",
        "In your lineup right now:",
    ]
    for alarm in alarms:
        lines.append(
            f"- **{alarm['player']}** (in your {alarm['slot']} slot) — "
            f"on a bye in week {week}, according to {' and '.join(alarm['sources'])}."
        )
    lines += [
        "",
        (
            "In ESPN, open **My Team**, and for each of them tap the player, choose "
            "**Move**, and swap him with someone from your bench who is playing this "
            "week. Do it before his game would have kicked off: ESPN locks each "
            "player individually at his own kickoff, not at one deadline for the week."
        ),
    ]

    season.write_advice(
        conn,
        kind="lineup",
        headline=headline,
        body="\n".join(lines),
        payload={"week": week, "on_bye": alarms},
        source_job=JOB_NAME,
    )


# --- the lineup card ---------------------------------------------------------


def _write_lineup(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    week: int | None,
    alarms: list[dict[str, Any]],
) -> None:
    """The lineup card, then the alarm on top of it.

    The alarm is written **last** on purpose: the feed is newest first, so the
    row written last is the row she sees first.
    """
    headline = str(payload.get("headline") or "").strip()
    label = f"Week {week}" if week else "This week"
    if not headline:
        headline = "Your lineup for this week"
    body = _lineup_body(payload)

    season.write_advice(
        conn,
        kind="lineup",
        headline=f"{label}: {headline}",
        body=body,
        payload={**payload, "week": week, "on_bye": alarms},
        source_job=JOB_NAME,
    )
    if alarms:
        _write_bye_alarm(conn, alarms, week)


def _lineup_body(payload: dict[str, Any]) -> str:
    lines = ["**Start these:**"]
    for entry in payload.get("starters") or []:
        lines.append(
            f"- {entry.get('slot')}: **{entry.get('player')}** — {entry.get('reason')}"
        )
    bench = payload.get("bench") or []
    if bench:
        lines += ["", "**Leave these on the bench:**"]
        for entry in bench:
            lines.append(f"- {entry.get('player')} — {entry.get('reason')}")
    lines += [
        "",
        (
            "In ESPN: **My Team**, then tap a player and choose **Move** to swap him "
            "with someone on your bench."
        ),
    ]
    return "\n".join(lines)


def _write_notes(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    notes = []
    for raw in payload.get("notes") or []:
        text = str(raw.get("text") or "").strip()
        if len(text) < 12:
            continue
        notes.append(
            memory.Note(
                text=text,
                source_job=JOB_NAME,
                topic="lineup",
                player_name=(str(raw.get("player_name") or "").strip() or None),
                source_url=(str(raw.get("source_url") or "").strip() or None),
            )
        )
    if notes:
        memory.write_notes(conn, notes)


def _summary(payload: dict[str, Any], alarms: list[dict[str, Any]], week: int | None) -> str:
    label = f"week {week}" if week else "this week"
    started = len(payload.get("starters") or [])
    line = f"{label}: {started} starters set"
    if alarms:
        names = ", ".join(alarm["player"] for alarm in alarms)
        line += f" — ON A BYE AND STILL IN THE LINEUP: {names}"
    return line


# --- prompt ------------------------------------------------------------------


def _prompt_values(
    conn: sqlite3.Connection,
    settings: Settings,
    roster: list[dict[str, Any]],
    week: int | None,
    stale: str | None,
) -> dict[str, Any]:
    league = _league(conn, settings)
    return {
        "week": week if week is not None else "unknown — work it out from the NFL schedule",
        "team_name": (league.name if league else None) or "her team",
        "team_count": league.team_count if league else "six",
        "scoring_summary": league.scoring_summary if league else "a point per catch (full PPR)",
        "starting_slots": _slot_lines(league),
        "roster": season.roster_lines(roster, week),
        "freshness": stale or "This roster was read from ESPN moments ago.",
    }


def _league(conn: sqlite3.Connection, settings: Settings) -> Any:
    try:
        return load_league_context(conn, settings)
    except LeagueUnknown:
        # A league we cannot describe is not a reason to skip the lineup check:
        # the roster is in front of us and the bye check does not need the
        # league's settings at all.
        log.warning("no league settings; the lineup prompt goes out with defaults")
        return None


def _slot_lines(league: Any) -> str:
    if league is None:
        return "  - unknown; use the roster slots shown against her players below"
    return "\n".join(f"  - {slot}: {count}" for slot, count in league.starting_slots.items())


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
