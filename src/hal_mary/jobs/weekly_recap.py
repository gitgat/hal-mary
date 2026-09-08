"""The weekly recap: what happened, what it meant, and one thing to learn.

Every other job in hal-mary tells Caroline what to do. This one is the only one
whose job is to make her need hal-mary less. Over sixteen weeks it is the
difference between "the app told me to start him" and "I know why he was the
right start", and that is worth more than any single week's advice.

So it **explains rather than reports**. A recap that says "you scored 112.4 and
lost by 6" is a scoreboard she has already seen in ESPN. A recap that says "you
lost because both your running backs play for teams that were losing badly, and a
team that is losing stops running the ball" teaches her something she can use in
week 9.

Two consequences in the code:

* The lesson is written to ``notes`` as well as to ``advice``, so a thing
  explained in week 3 is retrievable in week 9 rather than re-derived.
* Memory is retrieved with ``max_age_days=None``. Everywhere else in hal-mary a
  three-week-old note is stale and dangerous; here the subject *is* the season so
  far, and the default 21-day cutoff would hide most of it.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from hal_mary import memory, prompts
from hal_mary.config import Settings
from hal_mary.jobs import season
from hal_mary.jobs.registry import JobFailed, register
from hal_mary.league import LeagueUnknown, load_league_context

__all__ = ["JOB_NAME", "PROMPT_FILE", "RECAP_SCHEMA", "run"]

log = logging.getLogger(__name__)

JOB_NAME = "weekly_recap"
PROMPT_FILE = "weekly_recap.md"

#: How many of the week's own advice cards go into the prompt, so the recap can
#: say whether what hal-mary told her to do actually worked.
ADVICE_LOOKBACK = 12

RECAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "what_happened", "what_it_means", "lesson"],
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence. The most interesting true thing about her week.",
        },
        "what_happened": {
            "type": "string",
            "description": "A short paragraph. Concrete, about her actual players.",
        },
        "what_it_means": {
            "type": "string",
            "description": "A short paragraph about what it says about her team.",
        },
        "lesson": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "explanation"],
            "properties": {
                "title": {"type": "string", "description": "The idea, named."},
                "explanation": {
                    "type": "string",
                    "description": (
                        "Two or three sentences teaching it from scratch, using what "
                        "happened to her this week as the example."
                    ),
                },
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
}


@register(
    JOB_NAME,
    phases=("in_season",),
    summary="What happened last week, what it means, and one thing to learn.",
)
def run(
    conn: sqlite3.Connection, settings: Settings, runner: Any, client: Any = None
) -> str:
    """Write the recap. Raises :class:`JobFailed` when nothing usable came back."""
    stale = season.refresh_from_espn(conn, client)
    roster = season.my_roster(conn, settings)
    week = season.current_week(conn, settings, client)

    prompt = prompts.render_prompt(
        settings, PROMPT_FILE, _prompt_values(conn, settings, roster, week, stale)
    )
    context = memory.build_context(
        conn,
        settings,
        # Deliberately no player filter. Every other job asks about a named list
        # of players; the recap's subject is the season, and the lessons worth
        # carrying forward — "catches are worth a point here" — are filed against
        # no player at all and would be filtered straight out.
        note_limit=settings.research.note_limit,
        # The whole season, not the last three weeks. This is the one job whose
        # subject is everything that has happened, and the default cutoff would
        # hide most of it.
        max_age_days=None,
        extra_sections={"What hal-mary advised recently": _recent_advice(conn)},
    )

    result = runner.run(JOB_NAME, prompt, schema=RECAP_SCHEMA, extra_context=context)
    payload = season.structured_payload(result)
    if payload is None or not payload.get("what_happened"):
        log.warning(
            "weekly recap produced nothing usable; the model said: %s",
            (getattr(result, "text", "") or "")[:2000],
        )
        raise JobFailed(
            getattr(result, "error", None) or "the recap call returned nothing usable"
        )

    lesson = payload.get("lesson") or {}
    label = f"Week {week}" if week else "Last week"
    season.write_advice(
        conn,
        kind="recap",
        headline=f"{label}: {payload['headline']}",
        body=_body(payload, lesson),
        payload={**payload, "week": week},
        source_job=JOB_NAME,
    )
    _write_lesson(conn, lesson, payload)

    return f"{label} recap written: {lesson.get('title') or payload['headline']}"


def _body(payload: dict[str, Any], lesson: dict[str, Any]) -> str:
    lines = [payload["what_happened"]]
    if payload.get("what_it_means"):
        lines += ["", "**What that says about your team**", "", payload["what_it_means"]]
    if lesson.get("title") and lesson.get("explanation"):
        lines += ["", f"**One thing to learn: {lesson['title']}**", "", lesson["explanation"]]
    sources = [str(url) for url in (payload.get("sources") or []) if url]
    if sources:
        lines += ["", "Sources: " + ", ".join(sources)]
    return "\n".join(lines)


def _write_lesson(
    conn: sqlite3.Connection, lesson: dict[str, Any], payload: dict[str, Any]
) -> None:
    """Keep the lesson where later prompts can find it.

    Deliberately not given an ``expires_at``: "catches are worth a point here" is
    as true in December as it was in September, unlike the injury notes the news
    sweep writes.
    """
    title = str(lesson.get("title") or "").strip()
    explanation = str(lesson.get("explanation") or "").strip()
    if not explanation:
        return
    sources = [str(url) for url in (payload.get("sources") or []) if url]
    memory.write_note(
        conn,
        memory.Note(
            text=f"{title}: {explanation}" if title else explanation,
            source_job=JOB_NAME,
            topic="lesson",
            source_url=sources[0] if sources else None,
        ),
    )


def _recent_advice(conn: sqlite3.Connection) -> str:
    """The cards hal-mary put in front of her lately, so the recap can grade itself."""
    rows = conn.execute(
        "SELECT created_at, kind, headline, done FROM advice ORDER BY id DESC LIMIT ?",
        (ADVICE_LOOKBACK,),
    ).fetchall()
    if not rows:
        return ""
    return "\n".join(
        f"- [{(row['created_at'] or '')[:10]}] ({row['kind']}) {row['headline']}"
        + (" — she marked this done" if row["done"] else "")
        for row in rows
    )


def _prompt_values(
    conn: sqlite3.Connection,
    settings: Settings,
    roster: list[dict[str, Any]],
    week: int | None,
    stale: str | None,
) -> dict[str, Any]:
    try:
        league = load_league_context(conn, settings)
        scoring = league.scoring_summary
        team_count: Any = league.team_count
        team_name = league.name or "her team"
    except LeagueUnknown:
        scoring = "Every catch is worth 1 point on its own (full PPR)."
        team_count = "six"
        team_name = "her team"
    return {
        "week": week if week is not None else "the one that just finished",
        "team_name": team_name,
        "team_count": team_count,
        "scoring_summary": scoring,
        "roster": season.roster_lines(roster, week) or "  - nothing has synced yet.",
        "freshness": stale or "This roster was read from ESPN moments ago.",
    }
