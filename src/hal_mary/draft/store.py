"""Reading and writing the draft tables.

``board.py`` is deliberately pure — no database, no clock, no network — which is
what makes it cheap to test exhaustively. This module is the other half: it turns
SQLite rows into the plain dicts ``board.py`` expects and writes the results back.
The board-build job, the advisor and the draft loop all go through here, so there
is one definition of "what a board row looks like in memory".

**The one invariant worth stating out loud.** The ``board`` table has no
``drafted`` column: a row is drafted when a team owns it. But a pick entered by
hand knows the player and *not* the team, so a manual pick would write a row with
no team id and read back as still available — the player would be recommended
again on the next poll, forever. So :func:`mark_drafted` always writes
``drafted_at``, and :func:`load_board` treats a row with either marker as
drafted. A drafted row always has a timestamp; that is the invariant.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from hal_mary import db

log = logging.getLogger(__name__)

__all__ = [
    "all_picks",
    "identifies_a_player",
    "load_board",
    "mark_drafted",
    "next_overall_pick",
    "picks_for_team",
    "recent_picks",
    "record_unmatched",
    "replace_board",
    "store_draft_order",
    "stored_draft_order",
    "unmatched_picks",
]

_BOARD_COLUMNS = (
    "player_id, name, position, pro_team, tier, rank, bye_week, note, "
    "drafted_by_team_id, drafted_at, built_at"
)

_PICK_COLUMNS = "overall_pick, round_num, round_pick, team_id, player_id, player_name, seen_at"

#: A pick that actually names somebody.
#:
#: ESPN pre-populates every one of this league's 96 picks before the draft opens,
#: each with ``playerId: -1`` and no name — confirmed from the live payload on
#: 2026-09-07. Those rows identify nobody: they can never match a board row, and
#: counting them puts the next pick at 97, which every end-of-draft check reads
#: as "the draft is over" before it has begun. A hand-entered pick has no player
#: id at all and is kept, because it has a name.
_IDENTIFIED = "(player_name IS NOT NULL OR (player_id IS NOT NULL AND player_id > 0))"


def identifies_a_player(pick: dict[str, Any]) -> bool:
    """The Python half of :data:`_IDENTIFIED`, for picks already in memory."""
    if pick.get("player_name"):
        return True
    player_id = pick.get("player_id")
    return player_id is not None and player_id > 0


def load_board(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The whole board as plain dicts, best first, ready for ``board.py``.

    ``drafted`` is derived rather than stored: either marker means gone. See the
    module docstring for why both are needed.
    """
    rows = conn.execute(
        f"SELECT {_BOARD_COLUMNS} FROM board "
        "ORDER BY tier IS NULL, tier, rank IS NULL, rank, name"
    ).fetchall()
    return [
        {
            **dict(row),
            "drafted": row["drafted_by_team_id"] is not None or row["drafted_at"] is not None,
        }
        for row in rows
    ]


def replace_board(conn: sqlite3.Connection, rows: list[dict[str, Any]], built_at: str) -> int:
    """Swap the whole board for ``rows``. **Call inside ``db.transaction``.**

    Delete-then-insert, not a merge: the board is one coherent ranking produced
    by one research pass, and half of yesterday's ranking interleaved with half
    of today's is a board that says nothing. The caller owns the transaction so
    that a build which fails part-way leaves the old board standing rather than
    an empty table.

    Drafted markers are not carried over. Rebuilding the board mid-draft is not
    a supported move — it would forget who is gone — and the job that calls this
    runs the day before.
    """
    conn.execute("DELETE FROM board")
    conn.executemany(
        """
        INSERT INTO board
            (player_id, name, position, pro_team, tier, rank, bye_week, note, built_at)
        VALUES (:player_id, :name, :position, :pro_team, :tier, :rank, :bye_week, :note, :built_at)
        """,
        [
            {
                "player_id": row.get("player_id"),
                "name": row["name"],
                "position": row.get("position"),
                "pro_team": row.get("pro_team"),
                "tier": row.get("tier"),
                "rank": row.get("rank"),
                "bye_week": row.get("bye_week"),
                "note": row.get("note"),
                "built_at": built_at,
            }
            for row in rows
        ],
    )
    return len(rows)


def mark_drafted(conn: sqlite3.Connection, board: list[dict[str, Any]]) -> int:
    """Persist the drafted markers from an in-memory board; return rows changed.

    Only rows that ``apply_picks`` marked are written, and an attribution already
    in the database is never blanked: a hand-entered pick that knows the player
    and not the team must not erase the team ESPN supplied for the same row.
    """
    now = db.utc_now()
    updates = [
        {
            "player_id": entry.get("player_id"),
            "name": entry.get("name"),
            "team_id": entry.get("drafted_by_team_id"),
            # A drafted row always has a timestamp; see the module docstring.
            "drafted_at": entry.get("drafted_at") or now,
        }
        for entry in board
        if entry.get("drafted") or (entry.get("drafted_by_team_id") is not None)
    ]
    if not updates:
        return 0
    with db.transaction(conn):
        conn.executemany(
            """
            UPDATE board
               SET drafted_by_team_id = COALESCE(:team_id, drafted_by_team_id),
                   drafted_at         = COALESCE(drafted_at, :drafted_at)
             WHERE player_id = :player_id OR (:player_id IS NULL AND name = :name)
            """,
            updates,
        )
    return len(updates)


def record_unmatched(conn: sqlite3.Connection, picks: list[dict[str, Any]]) -> int:
    """Store picks the board could not place, for the draft page to surface.

    Idempotent per pick number, so a loop that re-reads the draft does not stack
    the same warning in front of Caroline once per poll.
    """
    if not picks:
        return 0
    now = db.utc_now()
    with db.transaction(conn):
        conn.executemany(
            """
            INSERT INTO unmatched_picks
                (overall_pick, team_id, player_id, player_name, seen_at, noticed_at)
            VALUES (:overall_pick, :team_id, :player_id, :player_name, :seen_at, :noticed_at)
            ON CONFLICT (overall_pick) DO UPDATE SET
                team_id     = excluded.team_id,
                player_id   = excluded.player_id,
                player_name = excluded.player_name
            """,
            [
                {
                    "overall_pick": pick.get("overall_pick"),
                    "team_id": pick.get("team_id"),
                    "player_id": pick.get("player_id"),
                    "player_name": pick.get("player_name") or pick.get("name"),
                    "seen_at": pick.get("seen_at"),
                    "noticed_at": now,
                }
                for pick in picks
            ],
        )
    return len(picks)


def unmatched_picks(
    conn: sqlite3.Connection, *, limit: int = 50, include_resolved: bool = False
) -> list[dict[str, Any]]:
    """Unmatched picks, newest first — what the draft page shows as a warning."""
    where = "" if include_resolved else "WHERE resolved_at IS NULL"
    rows = conn.execute(
        f"SELECT * FROM unmatched_picks {where} ORDER BY id DESC LIMIT ?", (max(int(limit), 0),)
    ).fetchall()
    return [dict(row) for row in rows]


def recent_picks(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
    """The last few picks, most recent first."""
    rows = conn.execute(
        f"SELECT {_PICK_COLUMNS} FROM draft_picks WHERE {_IDENTIFIED} "
        "ORDER BY overall_pick DESC LIMIT ?",
        (max(int(limit), 0),),
    ).fetchall()
    return [dict(row) for row in rows]


def all_picks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every pick recorded, in order. The loop reconciles the board against it.

    Small by construction — 96 rows in this league — so reading the lot on every
    poll costs nothing and removes the need to remember which picks have already
    been applied.
    """
    rows = conn.execute(f"SELECT {_PICK_COLUMNS} FROM draft_picks ORDER BY overall_pick").fetchall()
    return [dict(row) for row in rows]


def picks_for_team(conn: sqlite3.Connection, team_id: int | None) -> list[dict[str, Any]]:
    """Every pick a team has made, in order. ``None`` means "nobody"."""
    if team_id is None:
        return []
    rows = conn.execute(
        f"SELECT {_PICK_COLUMNS} FROM draft_picks WHERE team_id = ? ORDER BY overall_pick",
        (team_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def next_overall_pick(conn: sqlite3.Connection) -> int:
    """The 1-based overall pick about to be made.

    One past the highest pick that actually named somebody, which is 1 on an empty
    table — see :data:`_IDENTIFIED` for why "named somebody" and not "recorded".
    Reading the
    maximum rather than counting rows is what makes this right when ESPN reports
    picks out of order, and hand-entered picks get a number from SQLite's rowid
    for exactly the same reason.
    """
    row = conn.execute(
        f"SELECT MAX(overall_pick) AS highest FROM draft_picks WHERE {_IDENTIFIED}"
    ).fetchone()
    return int(row["highest"] or 0) + 1


# --- the order ESPN drew when the draft opened -------------------------------


def stored_draft_order(conn: sqlite3.Connection) -> list[int]:
    """Team ids by first-round slot, as ESPN's own draft board reported them.

    Empty until the draft opens and the loop reads that board — which is the
    honest answer, because until then the order has not been drawn. See
    ``migrations/004_draft_order.sql`` for why this is a table of its own.

    Never raises on an unmigrated database: this is read on the pick-clock path
    by :func:`hal_mary.league.load_league_context`, and "nothing stored" degrades
    to the pre-draft ``pickOrder``, which is what happened before this existed.
    """
    try:
        rows = conn.execute("SELECT team_id FROM draft_order ORDER BY slot").fetchall()
    except sqlite3.Error:  # pragma: no cover - an unmigrated database
        return []
    return [int(row["team_id"]) for row in rows]


def store_draft_order(conn: sqlite3.Connection, team_ids: list[int]) -> bool:
    """Record round one's slot-to-team mapping, **once**. Returns whether it wrote.

    Write-once by construction: an order already stored is left exactly as it is,
    so a later read that disagreed — ESPN is unofficial, and a loop restarted
    mid-draft reads the board again — cannot move Caroline's pick window while
    she is looking at it. The order is drawn once, so it is stored once.

    An empty list writes nothing rather than clearing what is there: "ESPN
    returned a board we could not read" must never demote a good stored order
    back to the placeholder.

    A second reading that **disagrees** is refused like any other, and logged.
    The single property this whole design protects is that the page, the card
    and the loop are reading the same order; when the source itself gives two
    answers, that is the one moment the property is in doubt, and it must not
    pass in silence. The warning names both orders so a person can tell which
    one ESPN's draft room agrees with — the stored one is what every screen is
    showing.
    """
    ids = [int(team_id) for team_id in team_ids]
    if not ids:
        return False
    already = stored_draft_order(conn)
    if already:
        if already != ids:
            log.warning(
                "ESPN's draft board now reads %s, but the order stored when the draft opened "
                "is %s; keeping the stored one, which is what the draft page and the advice "
                "card are using",
                ids,
                already,
            )
        return False
    now = db.utc_now()
    with db.transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO draft_order (slot, team_id, recorded_at) VALUES (?, ?, ?)",
            [(slot, team_id, now) for slot, team_id in enumerate(ids, start=1)],
        )
    return True
