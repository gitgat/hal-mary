"""Writing what ESPN says into SQLite, and into ``memory/league.md``.

Three rules, each paid for by something that happens on draft day.

**A failed sync leaves the previous snapshot intact.** Every network read
happens before any write, and every write happens inside one
:func:`hal_mary.db.transaction`. The web app showing yesterday's roster behind a
staleness banner is far better than showing nothing while the pick clock runs,
so a half-applied sync is never allowed to exist.

**Insert order is load-bearing.** ``roster_slots`` has real foreign keys to
``teams`` and ``players`` and connections from :func:`hal_mary.db.connect` have
foreign keys ON, so teams and players go in first or the insert raises.

**``sync_draft`` returns only picks that were new to this database.** The draft
loop triggers board updates and advice off that list. Returning every pick on
every five-second poll would re-advise Caroline on every tick.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from hal_mary import db
from hal_mary.config import Settings

__all__ = [
    "LEAGUE_MEMORY_FILENAME",
    "LEAGUE_PRESERVE_SENTINEL",
    "SYNC_RUN_KEEP",
    "last_sync",
    "league_memory_path",
    "sync_draft",
    "sync_league",
    "sync_players",
    "write_league_memory",
]

#: Everything from this line down in ``memory/league.md`` is hand-written and is
#: preserved verbatim by every sync. ``memory/league.md`` promises this in
#: writing and ``tests/unit/test_project_files.py`` pins the string.
LEAGUE_PRESERVE_SENTINEL = "<!-- hal-mary:preserve-below -->"

LEAGUE_MEMORY_FILENAME = "league.md"

#: How many ``sync_runs`` rows to keep per kind.
#:
#: The draft loop polls every five seconds and every poll writes a row — about
#: 720 an hour. Dropping the row for an uneventful poll is not an option: the
#: staleness banner reads the newest row, and without one per poll it could no
#: longer tell a quiet draft from a dead loop. So every run is still recorded and
#: the table is bounded instead. 200 rows is the last ~17 minutes of a live
#: draft, which is the window anyone actually looks at, and months of history
#: outside one.
SYNC_RUN_KEEP = 200

#: ``roster_slots.week`` for the current-roster snapshot. ESPN's "who is on this
#: team right now" is not a week, and NULL says so honestly; historical weeks get
#: real numbers when something starts loading them.
CURRENT_ROSTER_WEEK = None


# --- sync_runs bookkeeping -------------------------------------------------


def _sync_run_started(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (kind, started_at, status) VALUES (?, ?, 'running')",
        (kind, db.utc_now()),
    )
    run_id = cur.lastrowid
    if run_id is None:  # pragma: no cover - sqlite always reports a rowid here
        raise RuntimeError("sqlite did not report a row id for the new sync_runs row")
    return run_id


def _sync_run_finished(
    conn: sqlite3.Connection, run_id: int, kind: str, status: str, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
        (db.utc_now(), status, error, run_id),
    )
    _prune_sync_runs(conn, kind)


def _prune_sync_runs(conn: sqlite3.Connection, kind: str) -> None:
    """Keep this kind's newest :data:`SYNC_RUN_KEEP` rows and drop the rest.

    One indexed delete per sync, usually removing a single row. Only this kind is
    touched, so a draft poll cannot evict the league sync that the staleness
    banner is reporting on.

    A failure here is swallowed on purpose. This runs on the error path too, and
    a locked database during housekeeping must not replace the ESPN failure that
    is the actual news — an operator told "database is locked" while ESPN is
    down goes looking in the wrong place. The cost of losing a prune is that the
    table stays one row longer until the next sync.
    """
    try:
        conn.execute(
            """
            DELETE FROM sync_runs
            WHERE kind = ?
              AND id NOT IN (SELECT id FROM sync_runs WHERE kind = ? ORDER BY id DESC LIMIT ?)
            """,
            (kind, kind, SYNC_RUN_KEEP),
        )
    except sqlite3.Error:
        pass


def _require_no_open_transaction(conn: sqlite3.Connection, name: str) -> None:
    """Refuse to run inside a caller's transaction.

    Two reasons, and the second is the dangerous one. SQLite has no nested
    transactions, so the ``BEGIN`` below would raise anyway — but the
    ``sync_runs`` error row is written *outside* the data transaction precisely
    so it survives a rollback. Nested inside a caller's transaction it would be
    rolled back too, and a failed sync would leave no record that it ever ran.
    """
    if conn.in_transaction:
        raise RuntimeError(
            f"{name}() manages its own transaction and must not be called inside one; "
            "a sync nested in a caller's transaction loses its sync_runs record on failure"
        )


def last_sync(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    """The most recent sync of ``kind``, whatever its status.

    The staleness banner needs the newest attempt, not the newest success: "we
    tried ten minutes ago and ESPN said no" is the useful thing to show.
    """
    return conn.execute(
        "SELECT * FROM sync_runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
    ).fetchone()


# --- players ---------------------------------------------------------------


def _upsert_players(conn: sqlite3.Connection, players: Iterable[dict[str, Any]]) -> int:
    """Write player rows, last-writer-wins within the batch."""
    unique: dict[int, dict[str, Any]] = {}
    for player in players:
        player_id = player.get("player_id")
        name = player.get("name")
        # players.player_id and players.name are both NOT NULL. A player ESPN
        # will not name is not worth failing a whole sync over.
        if player_id is None or not name:
            continue
        unique[int(player_id)] = player

    now = db.utc_now()
    conn.executemany(
        """
        INSERT OR REPLACE INTO players
            (player_id, name, position, pro_team, injury_status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                player_id,
                player["name"],
                player.get("position"),
                player.get("pro_team"),
                player.get("injury_status"),
                now,
            )
            for player_id, player in unique.items()
        ],
    )
    return len(unique)


def sync_players(conn: sqlite3.Connection, players: Iterable[dict[str, Any]]) -> int:
    """Upsert player rows; returns how many distinct players were written.

    Shared by the roster and free-agent paths. Safe to call inside a caller's
    transaction — it opens its own only when there is not one already, because
    SQLite has no nested transactions.
    """
    if conn.in_transaction:
        return _upsert_players(conn, players)
    with db.transaction(conn):
        return _upsert_players(conn, players)


# --- league ----------------------------------------------------------------


def _write_league_settings(conn: sqlite3.Connection, settings: dict[str, Any]) -> None:
    # league_settings has CHECK(id = 1): exactly one row, ever.
    conn.execute(
        """
        INSERT OR REPLACE INTO league_settings
            (id, season, league_id, name, team_count, scoring_type, draft_type,
             draft_date, roster_slots_json, raw_json, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settings.get("season"),
            settings.get("league_id"),
            settings.get("name"),
            settings.get("team_count"),
            settings.get("scoring_type"),
            settings.get("draft_type"),
            settings.get("draft_date"),
            json.dumps(settings.get("roster_slots") or {}),
            settings.get("raw_json"),
            db.utc_now(),
        ),
    )


def _write_teams(conn: sqlite3.Connection, teams: Sequence[dict[str, Any]]) -> int:
    now = db.utc_now()
    written = [team for team in teams if team.get("team_id") is not None]
    conn.executemany(
        """
        INSERT OR REPLACE INTO teams (team_id, name, owner, abbrev, draft_slot, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                team["team_id"],
                team.get("name"),
                team.get("owner"),
                team.get("abbrev"),
                team.get("draft_slot"),
                now,
            )
            for team in written
        ],
    )
    return len(written)


def _write_roster_slots(conn: sqlite3.Connection, rosters: Sequence[dict[str, Any]]) -> int:
    """Replace the current-roster snapshot.

    This is the one delete-then-insert in the sync, and it is safe only because
    it runs inside the caller's transaction: a failure anywhere in the block
    rolls the delete back with everything else, so the previous snapshot is
    still there when the web app next reads it.

    It has to be a replace rather than an upsert. ``roster_slots`` is unique on
    ``(team_id, player_id, week)`` and the current snapshot has ``week`` NULL —
    and SQLite treats NULLs in a unique index as distinct, so ``ON CONFLICT``
    would never fire and every sync would add another copy of every player.
    """
    if not conn.in_transaction:  # pragma: no cover - callers all use transaction()
        raise RuntimeError("_write_roster_slots must run inside a transaction")

    now = db.utc_now()
    rows = [
        row
        for row in rosters
        if row.get("team_id") is not None and row.get("player_id") is not None
    ]
    conn.execute("DELETE FROM roster_slots WHERE week IS ?", (CURRENT_ROSTER_WEEK,))
    conn.executemany(
        """
        INSERT INTO roster_slots (team_id, player_id, slot, week, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (row["team_id"], row["player_id"], row.get("slot"), CURRENT_ROSTER_WEEK, now)
            for row in rows
        ],
    )
    return len(rows)


def sync_league(conn: sqlite3.Connection, client: Any) -> dict[str, Any]:
    """Pull settings, teams, rosters and free agents; write them; return counts.

    Every read happens before the transaction opens, so an ESPN failure never
    touches the database at all.
    """
    _require_no_open_transaction(conn, "sync_league")
    run_id = _sync_run_started(conn, "league")
    try:
        league_settings = client.league_settings()
        teams = client.teams()
        rosters = client.rosters()
        free_agents = client.free_agents()

        with db.transaction(conn):
            _write_league_settings(conn, league_settings)
            team_count = _write_teams(conn, teams)
            # Teams and players before roster slots: the foreign keys are real.
            player_count = _upsert_players(conn, [*rosters, *free_agents])
            slot_count = _write_roster_slots(conn, rosters)

        # Inside the try, and after the commit because it reads what was just
        # written. A sync whose memory file did not update is a partial failure:
        # memory/league.md is standing context on every Claude call, so an
        # OSError here must mark the run as failed rather than escape past a row
        # already recorded as successful.
        app_settings = getattr(client, "settings", None)
        if app_settings is not None:
            write_league_memory(conn, app_settings)
    except Exception as exc:
        _sync_run_finished(conn, run_id, "league", "error", str(exc))
        raise

    _sync_run_finished(conn, run_id, "league", "ok")

    return {
        "league": league_settings.get("name"),
        "teams": team_count,
        "players": player_count,
        "roster_slots": slot_count,
        "free_agents": len(free_agents),
    }


# --- draft -----------------------------------------------------------------


def _name_from_players_table(conn: sqlite3.Connection, player_id: int | None) -> str | None:
    if player_id is None:
        return None
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row["name"] if row else None


def sync_draft(conn: sqlite3.Connection, client: Any) -> list[dict[str, Any]]:
    """Upsert every pick ESPN reports; return **only** the ones new to this database.

    The draft loop's whole control flow hangs off that return value: a non-empty
    list means the board changed and advice may be worth running. An empty list
    means nothing happened, which is what almost every poll should say.

    Picks already stored keep their original ``seen_at``, so "when did we first
    see this pick" survives a correction to the pick itself.

    **Every pick here is a pick that happened.** ESPN pre-populates the whole
    draft board before a draft starts — 96 empty slots for a 6-team, 16-round
    league — and ``client.draft_picks()`` filters those out at the boundary, so
    ``draft_picks`` never holds a row for a slot nobody has drafted into. Until
    that filter existed the very first sync reported an entire draft in one tick.
    The empty slots are the pick schedule and are read from
    ``client.draft_schedule()`` instead; they are deliberately not persisted
    here, because which team owns a slot can change when the draft opens.
    """
    _require_no_open_transaction(conn, "sync_draft")
    run_id = _sync_run_started(conn, "draft")
    try:
        picks = client.draft_picks()

        known = {row["overall_pick"] for row in conn.execute("SELECT overall_pick FROM draft_picks")}
        resolved = [
            {
                **pick,
                "player_name": pick.get("player_name")
                or _name_from_players_table(conn, pick.get("player_id")),
            }
            for pick in picks
            if pick.get("overall_pick") is not None
        ]
        new = [pick for pick in resolved if pick["overall_pick"] not in known]

        now = db.utc_now()
        with db.transaction(conn):
            conn.executemany(
                """
                INSERT INTO draft_picks
                    (overall_pick, round_num, round_pick, team_id, player_id, player_name, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (overall_pick) DO UPDATE SET
                    round_num   = excluded.round_num,
                    round_pick  = excluded.round_pick,
                    team_id     = excluded.team_id,
                    player_id   = excluded.player_id,
                    player_name = excluded.player_name
                """,
                [
                    (
                        pick["overall_pick"],
                        pick.get("round_num"),
                        pick.get("round_pick"),
                        pick.get("team_id"),
                        pick.get("player_id"),
                        pick.get("player_name"),
                        now,
                    )
                    for pick in resolved
                ],
            )
    except Exception as exc:
        _sync_run_finished(conn, run_id, "draft", "error", str(exc))
        raise

    _sync_run_finished(conn, run_id, "draft", "ok")
    return new


# --- memory/league.md ------------------------------------------------------


def league_memory_path(settings: Settings) -> Path:
    """Where ``league.md`` lives.

    ``paths.memory_dir`` is already absolute — ``hal_mary.config`` anchors it to
    the directory holding ``config.toml``. This used to anchor a relative value
    against the repo root itself, which was a *second* answer to the same
    question: the sync would write here while ``standing_memory`` read somewhere
    else, and the file would exist and never reach a prompt.
    """
    return settings.paths.memory_dir / LEAGUE_MEMORY_FILENAME


DEFAULT_PRESERVED_TAIL = f"""{LEAGUE_PRESERVE_SENTINEL}

## Notes added by hand

Everything below the sentinel line is **preserved verbatim** by `hal-mary sync`.
Write anything here that ESPN does not know and hal-mary should: house rules,
what the commissioner said in the group chat, who trades fairly, which manager
never sets a lineup.

Do not delete the sentinel line — it is what tells the sync where the
machine-written half of this file ends.

_(nothing here yet)_
"""


def _describe_roster_slots(raw_json: str | None) -> str:
    """The starting lineup and bench, in the order ESPN lists them."""
    try:
        slots = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError:
        return "- Roster slots: unknown"
    if not slots:
        return "- Roster slots: unknown"
    return "\n".join(f"  - {name}: {count}" for name, count in slots.items())


def _ppr_sentence(raw_json: str | None) -> str | None:
    """Plain English for the one scoring detail that changes every ranking.

    ESPN's ``scoringType`` says ``H2H_POINTS``, which tells Caroline nothing.
    Whether a catch is worth a point is the thing that reorders the whole board,
    and it lives in ``scoringItems`` under stat 53, "Each reception".
    """
    try:
        raw = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError:
        return None
    for item in (raw.get("scoringSettings", {}) or {}).get("scoringItems", []) or []:
        if item.get("statId") == 53:
            points = item.get("points", 0)
            plural = "" if points == 1 else "s"
            if points >= 1:
                return f"Every catch is worth {points:g} point{plural} — this is a PPR league."
            if points > 0:
                return (
                    f"Every catch is worth {points:g} point{plural} — "
                    "this is a half-PPR league."
                )
            return "Catches are worth nothing on their own — this is standard scoring."
    return None


def _league_memory_body(conn: sqlite3.Connection, settings: Settings) -> str:
    row = conn.execute("SELECT * FROM league_settings WHERE id = 1").fetchone()
    if row is None:
        return (
            "# League — standing context\n\n"
            "**This league has not synced yet.** `hal-mary sync` writes everything above the\n"
            "sentinel line below, and it has not run successfully. hal-mary does not know the\n"
            "league name, its scoring, its roster slots or its draft date, and any advice that\n"
            "depends on them should say so rather than guess.\n\n"
            "---\n\n"
        )

    teams = list(conn.execute("SELECT * FROM teams ORDER BY draft_slot IS NULL, draft_slot, team_id"))
    ours = next((team for team in teams if team["team_id"] == settings.team_id), None)

    lines = [
        "# League — standing context",
        "",
        "Written by `hal-mary sync` from ESPN. Everything above the sentinel line below is",
        "rewritten on every sync; everything below it is hand-written and is preserved.",
        "",
        (
            f"- **League:** {row['name'] or 'unnamed'} "
            f"(ESPN league {row['league_id']}, {row['season']} season)"
        ),
        f"- **Size:** {row['team_count']} teams",
        f"- **Scoring type:** {row['scoring_type'] or 'unknown'}",
    ]

    ppr = _ppr_sentence(row["raw_json"])
    if ppr:
        lines.append(f"- **What that means:** {ppr}")

    lines += [
        (
            f"- **Draft:** {row['draft_type'] or 'unknown'} draft, "
            f"{row['draft_date'] or 'date not set'} (UTC)"
        ),
        "- **Roster slots:**",
        _describe_roster_slots(row["roster_slots_json"]),
        "",
    ]

    if ours is not None:
        slot = f", picking {ours['draft_slot']} in the first round" if ours["draft_slot"] else ""
        lines.append(
            f"**Caroline's team is \"{ours['name']}\" ({ours['abbrev']}), team id "
            f"{ours['team_id']}{slot}.**"
        )
    elif settings.team_id is None:
        lines.append("**TEAM_ID is not set, so hal-mary does not know which team is Caroline's.**")
    else:
        lines.append(
            f"**TEAM_ID is {settings.team_id}, but no such team was synced from ESPN.** "
            "Check the configuration."
        )

    if teams:
        lines += ["", "Teams in the league, in draft order:", ""]
        for team in teams:
            slot = team["draft_slot"] or "?"
            owner = team["owner"] or "unknown owner"
            lines.append(f"{slot}. {team['name']} ({team['abbrev']}) — {owner}")

    lines += ["", "---", ""]
    return "\n".join(lines) + "\n"


def write_league_memory(conn: sqlite3.Connection, settings: Settings) -> None:
    """Regenerate ``memory/league.md`` from the synced league settings.

    Claude reads this file as standing context on every call and Caroline reads
    it too, so it is written in plain English rather than as a data dump.

    **Everything from the sentinel down is preserved byte for byte.** The file
    as shipped promises that hand-written notes survive a sync; breaking that
    would quietly delete the one thing in this repo a human wrote by hand.
    """
    path = league_memory_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    index = existing.find(LEAGUE_PRESERVE_SENTINEL)
    preserved = existing[index:] if index != -1 else DEFAULT_PRESERVED_TAIL

    path.write_text(_league_memory_body(conn, settings) + preserved, encoding="utf-8")
