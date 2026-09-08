"""What the four in-season jobs all need: the roster, the week, and a place to
put an answer.

Each of the in-season jobs has the same skeleton — refresh what ESPN knows, read
Caroline's roster out of the database, put it in front of Claude with the notes
we already have, and write down what came back. This module is that skeleton's
shared parts, and nothing else. Football reasoning lives in ``prompts/*.md``;
what lives here is bookkeeping that would otherwise be copied four times and
drift three ways.

Two things here are load-bearing rather than convenient:

* :func:`bye_weeks` is the only place a bye week is looked up, and it matches by
  **name as well as id**. A board row researched before the first ESPN sync
  carries a synthetic negative id, so the id join finds nothing and the name is
  all there is. A bye we fail to look up is a starter who scores zero.
* :func:`refresh_from_espn` never raises. A Sunday-morning lineup check on a
  stale roster is worth a great deal; one that died because the cookies expired
  is worth nothing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from hal_mary import db
from hal_mary.draft.board import normalize_name

__all__ = [
    "BENCH_SLOTS",
    "HEALTHY",
    "bye_weeks",
    "current_week",
    "free_agent_lines",
    "is_starting",
    "my_roster",
    "player_names",
    "refresh_from_espn",
    "roster_lines",
    "structured_payload",
    "write_advice",
]

log = logging.getLogger(__name__)

#: Slots that are not in the lineup. A player here scores nothing and is meant
#: to — which is why a bye week on the bench is correct, not a problem.
BENCH_SLOTS = frozenset({"BE", "BN", "BENCH", "IR", "RES", "IR/RES", "TAXI"})

#: Injury statuses that mean "he is fine". Everything else is worth a mention.
HEALTHY = frozenset({"", "ACTIVE", "NORMAL", "PROBABLE"})


# --- reading what we know ----------------------------------------------------


def my_roster(conn: sqlite3.Connection, settings: Any) -> list[dict[str, Any]]:
    """Caroline's players, as the database currently has them.

    One row per player: ``name``, ``position``, ``pro_team``, ``injury_status``,
    ``slot``, ``player_id`` and the ``bye_week`` the board knows about. Starters
    first, then the bench, each group by slot — which is the order she reads her
    ESPN screen in, and therefore the order a lineup answer should come back in.

    An empty list is a real answer: it means nothing has synced yet, and every
    caller has to say so rather than advise about nobody.
    """
    team_id = _my_team_id(conn, settings)
    if team_id is None:
        return []

    rows = conn.execute(
        """
        SELECT p.player_id, p.name, p.position, p.pro_team, p.injury_status, r.slot
          FROM roster_slots r
          JOIN players p ON p.player_id = r.player_id
         WHERE r.team_id = ?
         ORDER BY p.name
        """,
        (team_id,),
    ).fetchall()

    byes = bye_weeks(conn)
    roster = [
        {
            "player_id": row["player_id"],
            "name": row["name"],
            "position": row["position"],
            "pro_team": row["pro_team"],
            "injury_status": (row["injury_status"] or "").upper(),
            "slot": row["slot"] or row["position"] or "BE",
            "starting": is_starting(row["slot"]),
            "bye_week": byes.get(normalize_name(row["name"])),
        }
        for row in rows
    ]
    # Starters first: the lineup question is about them, and a prompt that opens
    # with seven bench players buries it.
    roster.sort(key=lambda entry: (not entry["starting"], entry["slot"], entry["name"]))
    return roster


def _my_team_id(conn: sqlite3.Connection, settings: Any) -> int | None:
    """Her team id, from the league context, falling back to ``TEAM_ID``.

    ``load_league_context`` is the only sanctioned reader of league settings,
    but it raises when neither ESPN nor ``[league]`` has been filled in — and a
    job should not die of that when ``.env`` already says which team is hers.
    """
    from hal_mary.league import LeagueUnknown, load_league_context

    try:
        return load_league_context(conn, settings).my_team_id
    except LeagueUnknown:
        return settings.team_id


def is_starting(slot: str | None) -> bool:
    """Is this roster slot one that scores points this week?"""
    return (slot or "").strip().upper() not in BENCH_SLOTS


def bye_weeks(conn: sqlite3.Connection) -> dict[str, int]:
    """Normalized player name -> bye week, from every board row that has one.

    Keyed by *name*, not id, on purpose. The board is researched before the
    draft, so a row for a player who was never in ``players`` at build time
    carries a synthetic negative id that will never match a real roster row. The
    name is what survives, and ``normalize_name`` is the same spelling-tolerant
    key the draft code matches picks with.
    """
    byes: dict[str, int] = {}
    for row in conn.execute("SELECT name, bye_week FROM board WHERE bye_week IS NOT NULL"):
        try:
            week = int(row["bye_week"])
        except (TypeError, ValueError):
            continue
        if week > 0:
            byes[normalize_name(row["name"])] = week
    return byes


def current_week(conn: sqlite3.Connection, client: Any = None) -> int | None:
    """Which NFL week it is, from ESPN if it will say and the database if not.

    ``None`` is a real answer and callers have to handle it: with no week there
    is no bye check to make, and inventing a week from the calendar would flag
    the wrong players — which is worse than flagging none, because a wrong bye
    warning teaches her to ignore the right one.
    """
    if client is not None:
        try:
            week = getattr(client, "current_week", None)
            value = week() if callable(week) else week
            if value:
                return int(value)
        except Exception:
            log.warning("could not read the current week from ESPN", exc_info=True)

    row = conn.execute("SELECT MAX(week) AS week FROM roster_slots WHERE week IS NOT NULL").fetchone()
    if row is not None and row["week"]:
        return int(row["week"])
    return None


def player_names(roster: list[dict[str, Any]]) -> list[str]:
    """Just the names, for a memory lookup."""
    return [entry["name"] for entry in roster if entry.get("name")]


# --- refreshing what ESPN knows ----------------------------------------------


def refresh_from_espn(conn: sqlite3.Connection, client: Any) -> str | None:
    """Re-sync the league before a job reasons about it. Never raises.

    Returns ``None`` on success and a one-line reason when it did not happen, so
    a caller can put "the roster may be a few days old" in front of Claude
    rather than pretending it is fresh.

    Never raising is the whole point. Expired ESPN cookies are the most likely
    failure of the season, they happen without warning, and the answer to them
    on a Sunday morning is thinner advice, not silence.
    """
    if client is None:
        return "hal-mary has no ESPN credentials, so this is the last roster it was given."
    try:
        from hal_mary.espn import sync_league

        sync_league(conn, client)
    except Exception as exc:  # noqa: BLE001 - deliberate; see the docstring
        log.warning("could not refresh from ESPN before the job: %s", exc)
        return (
            f"The ESPN refresh failed ({type(exc).__name__}), so this is the last "
            "roster hal-mary was given and it may be a few days old."
        )
    return None


# --- rendering state into a prompt -------------------------------------------


def roster_lines(roster: list[dict[str, Any]], week: int | None = None) -> str:
    """The roster as Markdown bullets, starters and bench under their own headings.

    Bye weeks are spelled out — "on a BYE this week (week 6): he plays no game
    and scores zero" — rather than left as a number for the model to interpret.
    The prompt is allowed to be blunt here; this is the fact the whole job
    exists to surface.
    """
    starters = [entry for entry in roster if entry["starting"]]
    bench = [entry for entry in roster if not entry["starting"]]
    blocks = []
    for heading, group in (("In her lineup now", starters), ("On her bench now", bench)):
        if not group:
            continue
        lines = "\n".join(f"  - {_roster_line(entry, week)}" for entry in group)
        blocks.append(f"{heading}:\n{lines}")
    return "\n\n".join(blocks)


def _roster_line(entry: dict[str, Any], week: int | None) -> str:
    parts = [f"{entry['name']} ({entry.get('position') or '?'}"]
    if entry.get("pro_team"):
        parts.append(f", {entry['pro_team']}")
    parts.append(f") — roster slot {entry.get('slot')}")
    line = "".join(parts)
    bye = entry.get("bye_week")
    if bye:
        if week is not None and int(bye) == int(week):
            line += (
                f" — ON A BYE THIS WEEK (week {bye}): his real team does not play, "
                "so he would score zero"
            )
        else:
            line += f" — bye week {bye}"
    injury = (entry.get("injury_status") or "").upper()
    if injury and injury not in HEALTHY:
        line += f" — ESPN lists him as {injury}"
    return line


def free_agent_lines(free_agents: list[dict[str, Any]], limit: int = 40) -> str:
    """Available players as Markdown bullets, in the order ESPN returned them."""
    lines = []
    for entry in free_agents[:limit]:
        line = f"  - {entry.get('name')} ({entry.get('position') or '?'}"
        if entry.get("pro_team"):
            line += f", {entry['pro_team']}"
        line += ")"
        owned = entry.get("percent_owned")
        if owned is not None:
            line += f" — rostered in {owned:.0f}% of ESPN leagues"
        injury = (entry.get("injury_status") or "").upper()
        if injury and injury not in HEALTHY:
            line += f" — ESPN lists him as {injury}"
        lines.append(line)
    return "\n".join(lines)


# --- reading what came back --------------------------------------------------


def structured_payload(result: Any) -> dict[str, Any] | None:
    """The model's JSON object, from structured output or from the prose.

    A call that returned the right JSON with a non-zero exit code still returned
    the right JSON, and a research call is expensive enough to be worth
    rescuing. ``None`` means nothing usable came back at all.
    """
    structured = getattr(result, "structured", None)
    if isinstance(structured, dict) and structured:
        return structured
    text = (getattr(result, "text", "") or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        if isinstance(parsed, dict) and parsed:
            return parsed
    return None


def write_advice(
    conn: sqlite3.Connection,
    *,
    kind: str,
    headline: str,
    body: str | None,
    payload: dict[str, Any] | None,
    source_job: str,
) -> int:
    """One row in ``advice``, which is what the feed renders and she ticks off."""
    cur = conn.execute(
        """
        INSERT INTO advice (created_at, kind, headline, body, payload_json, source_job)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            db.utc_now(),
            kind,
            headline,
            body,
            json.dumps(payload) if payload is not None else None,
            source_job,
        ),
    )
    advice_id = cur.lastrowid
    if advice_id is None:  # pragma: no cover - sqlite always reports a rowid
        raise RuntimeError("sqlite did not report a row id for the new advice row")
    return advice_id
