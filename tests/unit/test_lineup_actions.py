"""Tests for the bye-week bench producer — the first action hal-mary emits.

A player whose real team does not play scores exactly zero. Nobody intends it,
the fix is one click, and the click is fully reversible. That makes it the one
recommendation that is safe to hand an executor with no judgement, and the whole
point of wiring it end to end is to prove the loop before anything irreversible
uses it.

The job is deterministic. No Claude call: the reasoning is arithmetic over the
roster and the board, and an action that fires without a model is one less thing
that can go wrong at 11am on a Sunday.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import FIXTURE_ENV
from hal_mary import actions, db
from hal_mary.config import ConfigError, load_settings
from hal_mary.jobs import lineup_actions

HER_TEAM_ID = 6
RIVAL_TEAM_ID = 1
CURRENT_WEEK = 5

ROSTER_SLOTS = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "D/ST": 1,
    "K": 1,
    "RB/WR/TE": 1,
    "BE": 7,
    "IR": 1,
}


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(env={**FIXTURE_ENV, "TEAM_ID": str(HER_TEAM_ID)})


def make_league(conn: sqlite3.Connection, week: int | None = CURRENT_WEEK) -> None:
    """A six-team league whose settings are synced, at ``week``."""
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO league_settings
                (id, season, league_id, name, team_count, scoring_type, draft_type,
                 roster_slots_json, raw_json, updated_at, current_week)
            VALUES (1, 2026, 7654321, 'The Invented League', 6, 'H2H_POINTS', 'SNAKE',
                    ?, '{}', '2026-10-01T12:00:00+00:00', ?)
            """,
            (json.dumps(ROSTER_SLOTS), week),
        )
        for team_id in (RIVAL_TEAM_ID, 2, 3, 4, 5, HER_TEAM_ID):
            conn.execute(
                "INSERT INTO teams (team_id, name, draft_slot) VALUES (?, ?, ?)",
                (team_id, f"Team {team_id}", team_id),
            )


def add_player(
    conn: sqlite3.Connection,
    player_id: int,
    name: str,
    position: str,
    slot: str,
    *,
    bye_week: int | None = None,
    rank: int | None = None,
    team_id: int = HER_TEAM_ID,
    injury_status: str = "ACTIVE",
) -> None:
    """One rostered player, with the board row that carries his bye week."""
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO players (player_id, name, position, pro_team, injury_status)
            VALUES (?, ?, ?, 'ATL', ?)
            """,
            (player_id, name, position, injury_status),
        )
        conn.execute(
            "INSERT INTO roster_slots (team_id, player_id, slot, week) VALUES (?, ?, ?, NULL)",
            (team_id, player_id, slot),
        )
        if bye_week is not None or rank is not None:
            conn.execute(
                """
                INSERT INTO board (player_id, name, position, tier, rank, bye_week)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (player_id, name, position, rank, bye_week),
            )


def a_full_healthy_lineup(conn: sqlite3.Connection) -> None:
    """Every starting slot filled by somebody who plays this week."""
    add_player(conn, 1, "Jayden Daniels", "QB", "QB", bye_week=9, rank=10)
    add_player(conn, 2, "Bijan Robinson", "RB", "RB", bye_week=9, rank=1)
    add_player(conn, 3, "De'Von Achane", "RB", "RB", bye_week=9, rank=2)
    add_player(conn, 4, "Puka Nacua", "WR", "WR", bye_week=9, rank=3)
    add_player(conn, 5, "Nico Collins", "WR", "WR", bye_week=9, rank=4)
    add_player(conn, 6, "Trey McBride", "TE", "TE", bye_week=9, rank=5)
    add_player(conn, 7, "Ravens D/ST", "D/ST", "D/ST", bye_week=9, rank=60)
    add_player(conn, 8, "Chris Boswell", "K", "K", bye_week=9, rank=70)
    add_player(conn, 9, "Chase Brown", "RB", "RB/WR/TE", bye_week=9, rank=6)


def emitted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM actions ORDER BY sequence, id"))


def notes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM notes ORDER BY id"))


# --- the happy path ----------------------------------------------------------


def test_a_started_player_on_bye_is_benched_for_a_legal_replacement(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    # Her starting running back is on bye this week; a back on her bench is not.
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Rhamondre Stevenson", "RB", "BE", bye_week=11, rank=30)

    result = lineup_actions.emit_bye_week_benchings(conn, settings)

    rows = emitted(conn)
    assert len(rows) == 1, [dict(row) for row in rows]
    row = rows[0]
    assert row["kind"] == "bench"
    assert row["player_name"] == "Bijan Robinson"
    assert row["slot"] == "RB"
    assert row["paired_player_name"] == "Rhamondre Stevenson"
    assert row["status"] == "pending"
    assert row["reversible"] == 1
    assert row["source_job"] == lineup_actions.JOB_NAME
    assert result["emitted"] == [row["id"]]


def test_the_reason_names_the_week_and_both_players_in_one_sentence(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Rhamondre Stevenson", "RB", "BE", bye_week=11, rank=30)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    reason = emitted(conn)[0]["reason"]
    assert "Bijan Robinson" in reason
    assert "Rhamondre Stevenson" in reason
    assert "week 5" in reason
    assert reason.count(".") == 1, f"the reason must be one sentence: {reason!r}"


def test_the_replacement_is_the_best_eligible_player_on_the_bench(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Worse Back", "RB", "BE", bye_week=11, rank=40)
    add_player(conn, 21, "Better Back", "RB", "BE", bye_week=11, rank=12)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn)[0]["paired_player_name"] == "Better Back"


def test_the_flex_slot_accepts_a_receiver(conn, settings):
    """This league spells its flex slot RB/WR/TE, and it takes any of the three."""
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 9", (CURRENT_WEEK,))
    add_player(conn, 20, "Jaylen Waddle", "WR", "BE", bye_week=11, rank=25)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    row = emitted(conn)[0]
    assert row["player_name"] == "Chase Brown"
    assert row["slot"] == "RB/WR/TE"
    assert row["paired_player_name"] == "Jaylen Waddle"


def test_two_starters_on_bye_do_not_get_the_same_replacement(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id IN (2, 3)", (CURRENT_WEEK,))
    add_player(conn, 20, "First Choice", "RB", "BE", bye_week=11, rank=12)
    add_player(conn, 21, "Second Choice", "RB", "BE", bye_week=11, rank=40)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    rows = emitted(conn)
    assert len(rows) == 2
    replacements = {row["paired_player_name"] for row in rows}
    assert replacements == {"First Choice", "Second Choice"}
    assert [row["sequence"] for row in rows] == [1, 2]


# --- the cases where doing nothing is right ----------------------------------


def test_no_legal_replacement_emits_nothing_and_says_why(conn, settings):
    """A bench with nobody to start in his place is worse than the bye."""
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 1", (CURRENT_WEEK,))
    # A receiver on the bench cannot fill a quarterback slot.
    add_player(conn, 20, "Jaylen Waddle", "WR", "BE", bye_week=11, rank=25)

    result = lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []
    assert result["emitted"] == []
    written = notes(conn)
    assert len(written) == 1
    assert written[0]["source_job"] == lineup_actions.JOB_NAME
    assert "Jayden Daniels" in written[0]["text"]
    assert written[0]["player_name"] == "Jayden Daniels"


def test_a_bench_player_who_is_himself_on_bye_is_not_a_legal_replacement(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Also On Bye", "RB", "BE", bye_week=CURRENT_WEEK, rank=12)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []
    assert len(notes(conn)) == 1


def test_a_bench_player_who_is_out_is_not_a_legal_replacement(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Hurt Back", "RB", "BE", bye_week=11, rank=12, injury_status="OUT")

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []


def test_a_benched_player_on_bye_is_left_alone(conn, settings):
    """He is already scoring nothing where he sits. There is nothing to do."""
    make_league(conn)
    a_full_healthy_lineup(conn)
    add_player(conn, 20, "Benched And On Bye", "RB", "BE", bye_week=CURRENT_WEEK, rank=30)
    add_player(conn, 21, "Available Back", "RB", "BE", bye_week=11, rank=12)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []
    assert notes(conn) == []


def test_a_player_on_the_injured_reserve_slot_is_not_a_starter(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    add_player(conn, 20, "Stashed", "RB", "IR", bye_week=CURRENT_WEEK, rank=30)
    add_player(conn, 21, "Available Back", "RB", "BE", bye_week=11, rank=12)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []


def test_another_teams_bye_week_starter_is_none_of_our_business(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    add_player(
        conn, 30, "Rival Starter", "RB", "RB", bye_week=CURRENT_WEEK, rank=8, team_id=RIVAL_TEAM_ID
    )
    add_player(conn, 20, "Available Back", "RB", "BE", bye_week=11, rank=12)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []


def test_a_player_with_no_board_row_has_no_known_bye_and_is_left_alone(conn, settings):
    """An unknown bye is not a bye. Guessing here benches somebody who plays."""
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("DELETE FROM board WHERE player_id = 2")
    add_player(conn, 20, "Available Back", "RB", "BE", bye_week=11, rank=12)

    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []


def test_an_unknown_current_week_emits_nothing_and_says_so(conn, settings):
    make_league(conn, week=None)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = 5 WHERE player_id = 2")
    add_player(conn, 20, "Available Back", "RB", "BE", bye_week=11, rank=12)

    result = lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []
    assert result["emitted"] == []
    assert "week" in result["summary"].lower()


def test_an_empty_roster_is_not_an_error(conn, settings):
    """Caroline's roster is empty until the draft happens."""
    make_league(conn)

    result = lineup_actions.emit_bye_week_benchings(conn, settings)

    assert emitted(conn) == []
    assert result["emitted"] == []


# --- idempotency -------------------------------------------------------------


def test_running_twice_emits_one_action(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Rhamondre Stevenson", "RB", "BE", bye_week=11, rank=30)

    first = lineup_actions.emit_bye_week_benchings(conn, settings)
    second = lineup_actions.emit_bye_week_benchings(conn, settings)

    assert len(emitted(conn)) == 1
    # The same action, and the second run says plainly that it wrote nothing.
    assert second["emitted"] == []
    assert second["already_queued"] == first["emitted"]
    assert len(actions.pending(conn)) == 1


def test_running_twice_writes_one_note_when_there_is_no_replacement(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 1", (CURRENT_WEEK,))

    lineup_actions.emit_bye_week_benchings(conn, settings)
    lineup_actions.emit_bye_week_benchings(conn, settings)

    assert len(notes(conn)) == 1


def test_the_job_records_a_run(conn, settings):
    make_league(conn)
    lineup_actions.emit_bye_week_benchings(conn, settings)

    row = conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["job"] == lineup_actions.JOB_NAME
    assert row["status"] == "ok"
    assert row["finished_at"]


# --- the deadline ------------------------------------------------------------


def test_every_emitted_action_expires_at_the_end_of_the_week_that_emitted_it(conn, settings):
    """Without a deadline there is no expiry and no other revocation path.

    Cowork's Sunday task does not run in week 5 because Claude Desktop was
    closed. Week 7 arrives, the bye is three weeks gone, Bijan Robinson is
    healthy and playing — and an instruction with no deadline is still pending,
    so a browser benches a starter unattended. The coarse boundary is not the
    eventual answer (that is a per-player kickoff), but it turns "actions expire"
    from a sentence in a design document into a mechanism.
    """
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Rhamondre Stevenson", "RB", "BE", bye_week=11, rank=30)
    sunday = datetime(2026, 10, 4, 10, 30, tzinfo=UTC)

    lineup_actions.emit_bye_week_benchings(conn, settings, now=sunday)

    assert emitted(conn)[0]["deadline"] == "2026-10-06T11:00:00+00:00"


def test_a_bench_queued_this_week_is_not_issued_two_weeks_later(conn, settings):
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Rhamondre Stevenson", "RB", "BE", bye_week=11, rank=30)
    sunday = datetime(2026, 10, 4, 10, 30, tzinfo=UTC)
    lineup_actions.emit_bye_week_benchings(conn, settings, now=sunday)

    two_weeks_later = datetime(2026, 10, 18, 10, 30, tzinfo=UTC)
    assert actions.pending(conn, now=two_weeks_later) == []


def test_running_twice_in_one_week_reports_the_second_as_already_queued(conn, settings):
    """`emit` returning an id says nothing about whether it wrote one."""
    make_league(conn)
    a_full_healthy_lineup(conn)
    conn.execute("UPDATE board SET bye_week = ? WHERE player_id = 2", (CURRENT_WEEK,))
    add_player(conn, 20, "Rhamondre Stevenson", "RB", "BE", bye_week=11, rank=30)
    sunday = datetime(2026, 10, 4, 10, 30, tzinfo=UTC)

    first = lineup_actions.emit_bye_week_benchings(conn, settings, now=sunday)
    second = lineup_actions.emit_bye_week_benchings(conn, settings, now=sunday)

    assert first["emitted"] and not first["already_queued"]
    assert not second["emitted"]
    assert second["already_queued"] == first["emitted"]
    assert len(emitted(conn)) == 1


def test_a_misconfigured_boundary_surfaces_instead_of_stopping_the_plan_quietly(conn, settings):
    """`refresh_after_sync` swallows a producer failure. Not a deployment one.

    A producer that could not plan this week should not fail a sync — the roster
    and the memory file are worth having. A config key that is simply wrong is a
    different animal: swallowed, it turns into "the plan silently stops
    refreshing", which is the failure mode this project keeps rediscovering.
    """
    make_league(conn)
    broken = settings.model_copy(
        update={"actions": settings.actions.model_copy(update={"week_boundary_weekday": "sunsday"})}
    )
    with pytest.raises(ConfigError):
        lineup_actions.refresh_after_sync(conn, broken)


def test_an_ordinary_producer_failure_still_leaves_the_sync_alone(conn, settings, monkeypatch):
    monkeypatch.setattr(
        lineup_actions,
        "emit_bye_week_benchings",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the board is missing")),
    )
    assert lineup_actions.refresh_after_sync(conn, settings) is None
