"""Tests for the SQLite layer: connection pragmas, migrations, job run bookkeeping.

Every test uses a real file on disk under ``tmp_path``. An in-memory database
would silently ignore the WAL pragma and would not exercise what production does.
"""

import sqlite3

import pytest

from hal_mary import db

EXPECTED_TABLES = {
    "schema_migrations",
    "league_settings",
    "teams",
    "players",
    "roster_slots",
    "draft_picks",
    "board",
    "notes",
    "notes_fts",
    "advice",
    "job_runs",
    "sync_runs",
    "claude_calls",
    "chat_sessions",
    "chat_messages",
}

# (table, exact tuple of indexed columns). Tuples, not substrings: a substring
# check against the index SQL matches the index *name*, so an index on the wrong
# column would pass, and a composite index would be satisfied by a single column.
EXPECTED_INDEXES = {
    ("notes", ("created_at",)),
    ("advice", ("created_at",)),
    ("advice", ("done",)),
    ("draft_picks", ("team_id",)),
    ("roster_slots", ("team_id",)),
    ("job_runs", ("job", "started_at")),
}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    yield connection
    connection.close()


def table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {row["name"] for row in rows}


# --- connect -----------------------------------------------------------------


def test_connect_creates_the_file_and_sets_row_factory(tmp_path):
    path = tmp_path / "hal.db"
    connection = db.connect(path)
    assert path.exists()
    row = connection.execute("SELECT 1 AS one").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["one"] == 1
    connection.close()


def test_connect_enables_foreign_keys_and_wal(tmp_path):
    connection = db.connect(tmp_path / "hal.db")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    connection.close()


def test_connect_accepts_a_string_path(tmp_path):
    connection = db.connect(str(tmp_path / "hal.db"))
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()


def test_connect_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "hal.db"
    connection = db.connect(path)
    assert path.exists()
    connection.close()


# --- migrate -----------------------------------------------------------------


def test_migrate_creates_every_expected_table(conn):
    assert EXPECTED_TABLES <= table_names(conn)


def test_migrate_records_the_applied_filenames(conn):
    rows = conn.execute("SELECT filename, applied_at FROM schema_migrations").fetchall()
    assert [row["filename"] for row in rows] == ["001_initial.sql"]
    assert rows[0]["applied_at"]


def test_migrate_returns_the_filenames_it_applied(tmp_path):
    connection = db.connect(tmp_path / "hal.db")
    assert db.migrate(connection) == ["001_initial.sql"]
    connection.close()


def test_migrate_twice_is_a_no_op(tmp_path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    assert db.migrate(connection) == []
    count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 1
    assert EXPECTED_TABLES <= table_names(connection)
    connection.close()


def test_migrate_applies_files_in_filename_order(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "002_second.sql").write_text("INSERT INTO ordering(step) VALUES ('second');")
    (migrations / "001_first.sql").write_text(
        "CREATE TABLE ordering(step TEXT); INSERT INTO ordering(step) VALUES ('first');"
    )
    (migrations / "notes.txt").write_text("not a migration")
    connection = db.connect(tmp_path / "hal.db")
    applied = db.migrate(connection, migrations_dir=migrations)
    assert applied == ["001_first.sql", "002_second.sql"]
    steps = [row["step"] for row in connection.execute("SELECT step FROM ordering")]
    assert steps == ["first", "second"]
    connection.close()


def test_a_failing_migration_rolls_back_and_is_not_recorded(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_bad.sql").write_text(
        "CREATE TABLE good(x INT); THIS IS NOT SQL;"
    )
    connection = db.connect(tmp_path / "hal.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(connection, migrations_dir=migrations)
    assert "good" not in table_names(connection)
    recorded = connection.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE filename = '001_bad.sql'"
    ).fetchone()[0]
    assert recorded == 0
    connection.close()


def index_column_tuples(conn, table):
    """Every index on `table`, as a tuple of its actual column names in order."""
    tuples = set()
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        columns = tuple(
            row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
        )
        tuples.add(columns)
    return tuples


@pytest.mark.parametrize(("table", "columns"), sorted(EXPECTED_INDEXES))
def test_expected_index_exists_on_the_right_columns(conn, table, columns):
    assert columns in index_column_tuples(conn, table)


def test_composite_job_runs_index_is_ordered_job_then_started_at(conn):
    """Order matters: (started_at, job) would not serve 'last run of job X'."""
    assert ("job", "started_at") in index_column_tuples(conn, "job_runs")
    assert ("started_at", "job") not in index_column_tuples(conn, "job_runs")


# --- schema behaviour ---------------------------------------------------------


def test_league_settings_allows_only_one_row(conn):
    conn.execute("INSERT INTO league_settings(id, season) VALUES (1, 2026)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO league_settings(id, season) VALUES (2, 2026)")


def test_foreign_keys_are_enforced_on_roster_slots(conn):
    conn.execute("INSERT INTO players(player_id, name) VALUES (10, 'Some Player')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO roster_slots(team_id, player_id, slot, week) VALUES (999, 10, 'WR', 1)"
        )


def test_roster_slots_accepts_a_valid_pair(conn):
    conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'Team One')")
    conn.execute("INSERT INTO players(player_id, name) VALUES (10, 'Some Player')")
    conn.execute(
        "INSERT INTO roster_slots(team_id, player_id, slot, week) VALUES (1, 10, 'WR', 1)"
    )
    assert conn.execute("SELECT COUNT(*) FROM roster_slots").fetchone()[0] == 1


def test_roster_slots_is_unique_per_team_player_week(conn):
    conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'Team One')")
    conn.execute("INSERT INTO players(player_id, name) VALUES (10, 'Some Player')")
    conn.execute(
        "INSERT INTO roster_slots(team_id, player_id, slot, week) VALUES (1, 10, 'WR', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO roster_slots(team_id, player_id, slot, week) VALUES (1, 10, 'FLEX', 1)"
        )


def test_players_name_is_required(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO players(player_id, name) VALUES (1, NULL)")


def test_draft_pick_reinsert_is_idempotent(conn):
    insert = (
        "INSERT OR REPLACE INTO draft_picks"
        "(overall_pick, round_num, round_pick, team_id, player_id, player_name, seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(insert, (13, 2, 1, 4, 555, "First Read", "2026-09-07T10:00:00"))
    conn.execute(insert, (13, 2, 1, 4, 555, "Second Read", "2026-09-07T10:00:05"))
    rows = conn.execute("SELECT * FROM draft_picks").fetchall()
    assert len(rows) == 1
    assert rows[0]["player_name"] == "Second Read"


def test_board_accepts_a_negative_synthetic_player_id(conn):
    conn.execute(
        "INSERT INTO board(player_id, name, position, tier, rank) VALUES (-1, 'Researched Guy', 'RB', 1, 3)"
    )
    assert conn.execute("SELECT name FROM board WHERE player_id = -1").fetchone()["name"] == (
        "Researched Guy"
    )


def test_advice_done_defaults_to_zero(conn):
    conn.execute(
        "INSERT INTO advice(created_at, kind, headline) VALUES ('2026-09-07T00:00:00', 'waiver', 'Claim X')"
    )
    assert conn.execute("SELECT done FROM advice").fetchone()["done"] == 0


def test_chat_messages_require_a_real_session(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, created_at)"
            " VALUES (42, 'user', 'hi', '2026-09-07T00:00:00')"
        )


# --- notes / FTS5 -------------------------------------------------------------


def insert_note(conn, text, player_name="Justin Jefferson", topic="injury"):
    cur = conn.execute(
        "INSERT INTO notes(created_at, source_job, topic, player_name, text, source_url)"
        " VALUES ('2026-09-07T00:00:00', 'news_sweep', ?, ?, ?, 'https://example.com/x')",
        (topic, player_name, text),
    )
    return cur.lastrowid


def fts_index_hits(conn, query):
    """Rows the FTS index alone returns, with no join back to notes.

    The join in fts_search() hides a stale index: once the notes row is deleted,
    a leftover index entry has nothing to join to and silently disappears from
    the result. Counting inside notes_fts is what actually exercises the
    AFTER DELETE trigger.
    """
    return conn.execute(
        "SELECT count(*) FROM notes_fts WHERE notes_fts MATCH ?", (query,)
    ).fetchone()[0]


def assert_fts_integrity(conn):
    """Ask FTS5 itself whether the index matches the content table.

    The `1` matters. Bare `('integrity-check')` only verifies the index against
    itself and passes happily with a stale entry pointing at a deleted note;
    `('integrity-check', 1)` is the form that compares against `notes`. Requires
    SQLite >= 3.41, which is older than the Python 3.12 this project needs.
    """
    conn.execute("INSERT INTO notes_fts(notes_fts, rank) VALUES ('integrity-check', 1)")


def fts_search(conn, query):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT n.id FROM notes n JOIN notes_fts ON notes_fts.rowid = n.id"
            " WHERE notes_fts MATCH ? ORDER BY rank, n.id",
            (query,),
        )
    ]


def test_fts_finds_a_note_after_insert(conn):
    note_id = insert_note(conn, "hamstring strain, questionable for week one")
    assert fts_search(conn, "hamstring") == [note_id]


def test_fts_matches_on_player_name_and_topic(conn):
    note_id = insert_note(conn, "some body text", player_name="Bijan Robinson", topic="usage")
    assert fts_search(conn, "Bijan") == [note_id]
    assert fts_search(conn, "usage") == [note_id]


def test_fts_reflects_an_update(conn):
    note_id = insert_note(conn, "hamstring strain")
    conn.execute("UPDATE notes SET text = 'fully cleared to play' WHERE id = ?", (note_id,))
    assert fts_search(conn, "hamstring") == []
    assert fts_search(conn, "cleared") == [note_id]


def test_fts_reflects_a_delete(conn):
    note_id = insert_note(conn, "hamstring strain")
    assert fts_index_hits(conn, "hamstring") == 1
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    assert fts_index_hits(conn, "hamstring") == 0
    assert fts_search(conn, "hamstring") == []


def test_fts_index_stays_consistent_with_notes(conn):
    """FTS5's own integrity check, after each kind of write."""
    first = insert_note(conn, "hamstring strain")
    insert_note(conn, "ankle sprain", player_name="Somebody Else", topic="usage")
    assert_fts_integrity(conn)
    conn.execute("UPDATE notes SET text = 'cleared to play' WHERE id = ?", (first,))
    assert_fts_integrity(conn)
    conn.execute("DELETE FROM notes WHERE id = ?", (first,))
    assert_fts_integrity(conn)


def test_fts_index_is_empty_after_deleting_every_note(conn):
    insert_note(conn, "hamstring strain")
    insert_note(conn, "hamstring tightness", player_name="Someone Else")
    conn.execute("DELETE FROM notes")
    assert fts_index_hits(conn, "hamstring") == 0
    assert_fts_integrity(conn)


def test_fts_ranks_and_returns_only_matching_notes(conn):
    first = insert_note(conn, "ankle sprain limits snaps")
    insert_note(conn, "signed a contract extension", player_name="Nobody", topic="business")
    assert fts_search(conn, "ankle") == [first]


def test_notes_text_is_required(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notes(created_at, text) VALUES ('2026-09-07T00:00:00', NULL)"
        )


# --- job runs -----------------------------------------------------------------


def test_job_run_started_writes_a_running_row(conn):
    run_id = db.job_run_started(conn, "board_build")
    row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["job"] == "board_build"
    assert row["status"] == "running"
    assert row["started_at"]
    assert row["finished_at"] is None
    assert row["summary"] is None
    assert row["error"] is None


def test_job_run_started_returns_distinct_ids(conn):
    assert db.job_run_started(conn, "a") != db.job_run_started(conn, "b")


def test_job_run_finished_completes_the_row(conn):
    run_id = db.job_run_started(conn, "news_sweep")
    db.job_run_finished(conn, run_id, "ok", summary="14 notes written")
    row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["summary"] == "14 notes written"
    assert row["error"] is None
    assert row["finished_at"]


def test_job_run_finished_records_an_error(conn):
    run_id = db.job_run_started(conn, "waiver_scan")
    db.job_run_finished(conn, run_id, "error", error="claude exited 1")
    row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "error"
    assert row["error"] == "claude exited 1"


def test_job_run_finished_rejects_an_unknown_status(conn):
    run_id = db.job_run_started(conn, "chat")
    with pytest.raises(ValueError, match="status"):
        db.job_run_finished(conn, run_id, "finished-i-guess")


def test_job_run_finished_rejects_an_unknown_run_id(conn):
    with pytest.raises(LookupError):
        db.job_run_finished(conn, 9999, "ok")


# --- NOT NULL on the ESPN-supplied primary keys -------------------------------


def test_team_id_cannot_be_null(conn):
    """`INT PRIMARY KEY` is not a rowid alias, so SQLite's legacy quirk would
    otherwise let several NULL-id teams in and break every join."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO teams(team_id, name) VALUES (NULL, 'Nameless')")


def test_player_id_cannot_be_null(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO players(player_id, name) VALUES (NULL, 'Some Player')")


# --- transaction() ------------------------------------------------------------


def test_transaction_commits_on_success(tmp_path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    with db.transaction(connection):
        connection.execute("INSERT INTO teams(team_id, name) VALUES (1, 'One')")
        connection.execute("INSERT INTO teams(team_id, name) VALUES (2, 'Two')")
    connection.close()

    reopened = db.connect(tmp_path / "hal.db")
    assert reopened.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 2
    reopened.close()


def test_transaction_rolls_back_every_statement_on_failure(conn):
    conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'Existing')")
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO teams(team_id, name) VALUES (2, 'Two')")
        conn.execute("INSERT INTO teams(team_id, name) VALUES (3, 'Three')")
        # duplicate primary key: the whole block must be undone
        conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'Clash')")
    names = [row["name"] for row in conn.execute("SELECT name FROM teams")]
    assert names == ["Existing"]


def test_transaction_reraises_the_original_exception(conn):
    class Boom(Exception):
        pass

    with pytest.raises(Boom), db.transaction(conn):
        conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'One')")
        raise Boom("something in the caller failed")
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0


def test_transaction_yields_the_connection(conn):
    with db.transaction(conn) as handle:
        assert handle is conn


def test_transaction_leaves_no_open_transaction_behind(conn):
    with db.transaction(conn):
        conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'One')")
    assert not conn.in_transaction
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'Clash')")
    assert not conn.in_transaction


# --- rollback must never replace the error that caused it ---------------------


def test_transaction_rollback_never_replaces_the_original_error(conn):
    """SQLite auto-rolls-back on SQLITE_FULL / IOERR / NOMEM. A bare ROLLBACK
    afterwards raises "cannot rollback - no transaction is active" and the real
    error never reaches the operator. The explicit ROLLBACK below stands in for
    the automatic one."""

    class DiskFull(Exception):
        pass

    with pytest.raises(DiskFull, match="the real failure"), db.transaction(conn):
        conn.execute("INSERT INTO teams(team_id, name) VALUES (1, 'One')")
        conn.execute("ROLLBACK")  # SQLite has already unwound the transaction
        raise DiskFull("the real failure")


def test_a_migration_that_lost_its_transaction_still_reports_its_own_error(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_unwound.sql").write_text(
        "CREATE TABLE t (x INT);\nROLLBACK;\nTHIS IS NOT SQL;"
    )
    connection = db.connect(tmp_path / "hal.db")
    with pytest.raises(sqlite3.Error) as excinfo:
        db.migrate(connection, migrations_dir=migrations)
    assert "syntax error" in str(excinfo.value)
    assert "no transaction is active" not in str(excinfo.value)
    connection.close()
