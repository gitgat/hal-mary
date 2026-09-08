"""The waiver scan: who is worth claiming, who to drop for him, and by when.

Runs Tuesday, after the previous week has finished and before ESPN processes the
week's claims — the only moment in the week when everybody's waiver priority is
still intact.

Three things this job does that a list of names would not:

* **One ``advice`` row per claim.** She reads these on a phone, one card at a
  time, and ticks off what she has actioned. A single card listing five claims is
  one thing to tick and four things to forget.
* **Every row states the deadline.** A card without it is a card she reads on
  Wednesday afternoon and acts on too late.
* **A drop is checked against her actual roster.** Telling her to drop somebody
  she does not own is an instruction she cannot follow, and one of those makes
  every other row on the page less trustworthy.

A week with nothing worth claiming is a real answer, not a failure: it returns a
summary and writes no rows.
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

__all__ = ["JOB_NAME", "PROMPT_FILE", "WAIVER_SCHEMA", "run"]

log = logging.getLogger(__name__)

JOB_NAME = "waiver_scan"
PROMPT_FILE = "waiver_scan.md"

WAIVER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["deadline", "claims"],
    "properties": {
        "deadline": {
            "type": "string",
            "description": (
                "When these claims stop being possible, in words — e.g. "
                "'Wednesday morning, when ESPN processes this week's claims'."
            ),
        },
        "headline": {"type": "string", "description": "One sentence about the week."},
        "claims": {
            "type": "array",
            "description": "Ranked, best first. An empty list is a valid answer.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["add", "reason", "urgency", "bid"],
                "properties": {
                    "add": {"type": "string", "description": "Who to claim, as ESPN spells him."},
                    "position": {"type": "string"},
                    "drop": {
                        "type": ["string", "null"],
                        "description": (
                            "Who to drop to make room, from her roster only, or null "
                            "if she has a free spot."
                        ),
                    },
                    "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                    "bid": {
                        "type": "string",
                        "description": "How hard to go after him, in words she can act on.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One or two sentences a beginner understands.",
                    },
                    "source_url": {"type": "string"},
                },
            },
        },
    },
}


@register(
    JOB_NAME,
    phases=("in_season",),
    summary="Free agents worth claiming, ranked, with who to drop and by when.",
)
def run(
    conn: sqlite3.Connection, settings: Settings, runner: Any, client: Any = None
) -> str:
    """Scan the wire. Raises :class:`JobFailed` when the call returns nothing usable."""
    stale = season.refresh_from_espn(conn, client)
    roster = season.my_roster(conn, settings)
    if not roster:
        raise JobFailed(
            "there is no roster in the database yet, so there is nothing to improve. "
            "Run `hal-mary sync` first."
        )

    free_agents = _free_agents(settings, client)
    if not free_agents:
        raise JobFailed(
            "hal-mary could not read the list of available players from ESPN, so it "
            "cannot say who is worth claiming. Check the ESPN cookies on the status page."
        )

    prompt = prompts.render_prompt(
        settings,
        PROMPT_FILE,
        _prompt_values(conn, settings, roster, free_agents, stale, client),
    )
    context = memory.build_context(
        conn,
        settings,
        players=[
            *season.player_names(roster),
            *[str(row.get("name")) for row in free_agents[:20] if row.get("name")],
        ],
        topics=["injury", "news", "role", "waiver"],
        note_limit=settings.research.note_limit,
    )

    result = runner.run(JOB_NAME, prompt, schema=WAIVER_SCHEMA, extra_context=context)
    payload = season.structured_payload(result)
    if payload is None or "claims" not in payload:
        log.warning(
            "waiver scan produced nothing usable; the model said: %s",
            (getattr(result, "text", "") or "")[:2000],
        )
        raise JobFailed(
            getattr(result, "error", None) or "the waiver scan returned nothing usable"
        )

    deadline = str(payload.get("deadline") or "").strip() or (
        "before ESPN processes this week's claims"
    )
    claims = _claims(payload, roster, settings)
    if not claims:
        return "nothing on the wire is worth claiming this week"

    for rank, claim in enumerate(claims, start=1):
        season.write_advice(
            conn,
            kind="waiver",
            headline=_headline(claim, rank),
            body=_body(claim, deadline),
            payload={**claim, "rank": rank, "deadline": deadline},
            source_job=JOB_NAME,
        )

    return f"{len(claims)} claims worth making, best first: {claims[0]['add']}"


# --- cleaning what came back -------------------------------------------------


def _claims(
    payload: dict[str, Any], roster: list[dict[str, Any]], settings: Settings
) -> list[dict[str, Any]]:
    """The model's claims, trimmed and checked against what she actually owns."""
    owned = {normalize_name(entry["name"]): entry["name"] for entry in roster}
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in payload.get("claims") or []:
        if not isinstance(raw, dict):
            continue
        add = str(raw.get("add") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not add or not reason:
            continue
        key = normalize_name(add)
        if key in seen:
            continue
        seen.add(key)

        drop = str(raw.get("drop") or "").strip()
        # A drop she does not own is an instruction she cannot follow. Dropping
        # the field beats dropping the claim: the player is still worth having,
        # and she can pick somebody to release herself.
        if drop and normalize_name(drop) not in owned:
            log.warning("waiver scan suggested dropping %r, who is not on her roster", drop)
            drop = ""

        cleaned.append(
            {
                "add": add,
                "position": (str(raw.get("position") or "").strip().upper() or None),
                "drop": owned.get(normalize_name(drop)) if drop else None,
                "urgency": (str(raw.get("urgency") or "medium").strip().lower() or "medium"),
                "bid": str(raw.get("bid") or "").strip(),
                "reason": reason,
                "source_url": (str(raw.get("source_url") or "").strip() or None),
            }
        )
        if len(cleaned) >= settings.research.waiver_claims:
            break
    return cleaned


def _headline(claim: dict[str, Any], rank: int) -> str:
    lead = "Claim" if rank == 1 else f"Claim #{rank}"
    line = f"{lead}: pick up {claim['add']}"
    if claim["drop"]:
        line += f", drop {claim['drop']}"
    return line


def _body(claim: dict[str, Any], deadline: str) -> str:
    lines = [claim["reason"], ""]
    if claim["bid"]:
        lines += [f"**How hard to go after him:** {claim['bid']}", ""]
    if claim["drop"]:
        lines.append(
            f"**Who to drop:** {claim['drop']}. ESPN asks you to pick somebody to "
            "release when your roster is full."
        )
    else:
        lines.append("**Who to drop:** nobody — you have a free spot, so just add him.")
    lines += [
        "",
        f"**Do this by:** {deadline}.",
        "",
        (
            "In ESPN: open **Players**, search for him, and tap **Add** — or **Claim** "
            "if the waiver period is still running. A claim is not instant: "
            "everybody's claims are settled together, and whoever has the higher "
            "priority or bid gets him."
        ),
    ]
    if claim["source_url"]:
        lines += ["", f"Source: {claim['source_url']}"]
    return "\n".join(lines)


# --- prompt ------------------------------------------------------------------


def _free_agents(settings: Settings, client: Any) -> list[dict[str, Any]]:
    if client is None:
        return []
    try:
        return list(client.free_agents(size=settings.research.free_agent_size))
    except Exception:
        log.warning("could not read the free agent list", exc_info=True)
        return []


def _prompt_values(
    conn: sqlite3.Connection,
    settings: Settings,
    roster: list[dict[str, Any]],
    free_agents: list[dict[str, Any]],
    stale: str | None,
    client: Any,
) -> dict[str, Any]:
    week = season.current_week(conn, client)
    try:
        league = load_league_context(conn, settings)
        scoring = league.scoring_summary
        team_count: Any = league.team_count
        slots = "\n".join(f"  - {slot}: {n}" for slot, n in league.starting_slots.items())
    except LeagueUnknown:
        scoring = "Every catch is worth 1 point on its own (full PPR)."
        team_count = "six"
        slots = "  - unknown; use the roster slots shown against her players below"
    return {
        "week": week if week is not None else "unknown — work it out from the NFL schedule",
        "team_count": team_count,
        "scoring_summary": scoring,
        "starting_slots": slots,
        "max_claims": settings.research.waiver_claims,
        "roster": season.roster_lines(roster, week),
        "free_agents": season.free_agent_lines(
            free_agents, settings.research.free_agent_shortlist
        ),
        "freshness": stale or "This roster was read from ESPN moments ago.",
    }
