"""Tests for ``hal-mary backup``.

The database holds the season's accumulated notes, and that is the one thing in
this project that cannot be regenerated: the ESPN state resyncs, the board
rebuilds, the notes do not. So the backup has to be right in the two ways
backups are usually wrong.

**It must not be a file copy.** The database is in WAL mode and the service
writes to it while the backup runs. ``cp`` captures the main file without the
write-ahead log, which is a torn snapshot that opens cleanly and is missing the
most recent — most interesting — writes. These tests write *during* a backup to
pin that.

**It must prune.** A backup that fills the disk takes the service down, which is
a worse outcome than the one it was insuring against.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hal_mary import db
from hal_mary.backup import STAMP_FORMAT, backup_database, backup_files
from hal_mary.config import load_settings

MINIMAL_TOML = """
[claude]
binary = "claude"
default_model = "sonnet-test"
permission_mode = "dontAsk"
scratch_dir = ".scratch"
system_prompt_file = "prompts/system.md"

[paths]
prompts_dir = "prompts"
memory_dir = "memory"

[draft]
poll_seconds = 5
advise_within_picks = 2

[web]
host = "0.0.0.0"
port = 8080
session_cookie = "hal_mary_session"

[backup]
keep = 3
"""


@pytest.fixture
def live_db(tmp_path: Path):
    """A migrated database with a note in it, and the settings that point at it."""
    (tmp_path / "config.toml").write_text(MINIMAL_TOML, encoding="utf-8")
    settings = load_settings(
        config_path=tmp_path / "config.toml",
        env={"DB_PATH": str(tmp_path / "data" / "hal.db")},
    )
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO notes (created_at, source_job, player_name, text)"
            " VALUES (?, 'board_build', 'Bijan Robinson', 'a note worth keeping')",
            (db.utc_now(),),
        )
    yield settings, conn
    conn.close()


def open_backup(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# --- the backup itself -------------------------------------------------------


def test_the_backup_opens_as_a_database_with_the_expected_tables(live_db):
    settings, _conn = live_db
    result = backup_database(settings)

    assert result.path.is_file()
    backup = open_backup(result.path)
    try:
        tables = {
            row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "notes" in tables
        assert "schema_migrations" in tables
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup.close()


def test_the_backup_carries_the_rows(live_db):
    settings, _conn = live_db
    result = backup_database(settings)

    backup = open_backup(result.path)
    try:
        rows = backup.execute("SELECT player_name, text FROM notes").fetchall()
    finally:
        backup.close()

    assert [(r["player_name"], r["text"]) for r in rows] == [
        ("Bijan Robinson", "a note worth keeping")
    ]


def test_a_write_still_in_the_wal_reaches_the_backup(live_db):
    """The reason this is not ``cp``.

    In WAL mode a committed row can live entirely in ``hal.db-wal`` with nothing
    of it in ``hal.db``. A file copy of the main database alone is a snapshot
    that opens fine and has silently lost the newest notes — which are the ones
    the season is made of.
    """
    settings, conn = live_db
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO notes (created_at, source_job, player_name, text)"
            " VALUES (?, 'board_build', 'Puka Nacua', 'written after the checkpoint')",
            (db.utc_now(),),
        )

    result = backup_database(settings)

    backup = open_backup(result.path)
    try:
        subjects = {r[0] for r in backup.execute("SELECT player_name FROM notes")}
    finally:
        backup.close()
    assert "Puka Nacua" in subjects


def test_the_backup_is_a_single_file_with_no_sidecars(live_db):
    """A backup that needs its -wal beside it is a backup someone will lose."""
    settings, _conn = live_db
    result = backup_database(settings)

    assert not result.path.with_name(result.path.name + "-wal").exists()
    assert not result.path.with_name(result.path.name + "-shm").exists()


def test_the_source_database_is_untouched(live_db):
    settings, conn = live_db
    before = conn.execute("SELECT count(*) FROM notes").fetchone()[0]

    backup_database(settings)

    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == before


def test_the_filename_is_a_sortable_utc_timestamp(live_db):
    settings, _conn = live_db
    when = datetime(2026, 11, 3, 4, 17, 0, tzinfo=UTC)

    result = backup_database(settings, now=when)

    assert result.path.name == f"hal-{when.strftime(STAMP_FORMAT)}.db"
    assert "20261103T041700Z" in result.path.name


def test_the_default_destination_sits_beside_the_database(live_db):
    """Not in the checkout: a deploy wipes a checkout, and the point of a backup
    is to survive exactly that."""
    settings, _conn = live_db

    result = backup_database(settings)

    assert result.path.parent == settings.db_path.parent / "backups"


def test_the_destination_is_created_if_absent(live_db):
    settings, _conn = live_db
    dest = settings.db_path.parent / "elsewhere" / "deeper"

    result = backup_database(settings, dest_dir=dest)

    assert result.path.parent == dest


def test_backing_up_a_database_that_does_not_exist_is_an_error(tmp_path):
    (tmp_path / "config.toml").write_text(MINIMAL_TOML, encoding="utf-8")
    settings = load_settings(
        config_path=tmp_path / "config.toml", env={"DB_PATH": str(tmp_path / "gone.db")}
    )

    with pytest.raises(FileNotFoundError):
        backup_database(settings)


def test_a_failed_backup_leaves_no_half_written_file(live_db, monkeypatch):
    """A partial file in the backup directory is worse than no file: it is the
    one that gets restored.

    The failure is injected at ``sqlite3.connect`` rather than at
    ``Connection.backup``, which belongs to an immutable C type. Failing the
    *source* connection means the destination file has already been created by
    the time the error lands, so this exercises the real cleanup path rather
    than a case where nothing was written anyway.
    """
    from hal_mary import backup as backup_module

    settings, _conn = live_db
    real_connect = sqlite3.connect

    class FailsMidCopy:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def backup(self, *args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            self._wrapped.close()

    def connect(target, *args, **kwargs):
        conn = real_connect(target, *args, **kwargs)
        return FailsMidCopy(conn) if "mode=ro" in str(target) else conn

    monkeypatch.setattr(backup_module.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError):
        backup_database(settings)

    dest = settings.db_path.parent / "backups"
    assert backup_files(dest, settings.db_path) == []
    assert list(dest.glob("*" + backup_module.PARTIAL_SUFFIX)) == []


# --- retention ---------------------------------------------------------------


def test_old_backups_are_pruned_to_the_configured_window(live_db):
    settings, _conn = live_db  # config.toml above sets keep = 3
    stamps = [datetime(2026, 11, day, 4, 17, tzinfo=UTC) for day in range(1, 7)]

    results = [backup_database(settings, now=when) for when in stamps]

    kept = sorted(p.name for p in backup_files(results[-1].path.parent, settings.db_path))
    assert len(kept) == 3
    assert kept == sorted(r.path.name for r in results[-3:])


def test_keep_can_be_overridden_at_the_call(live_db):
    settings, _conn = live_db
    stamps = [datetime(2026, 11, day, 4, 17, tzinfo=UTC) for day in range(1, 6)]

    for when in stamps:
        result = backup_database(settings, now=when, keep=2)

    assert len(backup_files(result.path.parent, settings.db_path)) == 2


def test_the_newest_backup_is_never_the_one_pruned(live_db):
    settings, _conn = live_db
    newest = backup_database(settings, now=datetime(2026, 12, 25, 4, 17, tzinfo=UTC), keep=1)

    assert newest.path.is_file()


def test_pruning_ignores_files_that_are_not_ours(live_db):
    """The backup directory is a directory on someone's disk. Deleting a file we
    did not write is not a mistake anyone gets to make twice."""
    settings, _conn = live_db
    dest = settings.db_path.parent / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    bystander = dest / "important-notes.txt"
    bystander.write_text("not a backup", encoding="utf-8")

    for day in range(1, 6):
        backup_database(settings, now=datetime(2026, 11, day, 4, 17, tzinfo=UTC), keep=1)

    assert bystander.is_file()


def test_the_result_reports_what_it_wrote_and_what_it_removed(live_db):
    settings, _conn = live_db
    backup_database(settings, now=datetime(2026, 11, 1, 4, 17, tzinfo=UTC), keep=1)
    result = backup_database(settings, now=datetime(2026, 11, 2, 4, 17, tzinfo=UTC), keep=1)

    assert result.size_bytes > 0
    assert [p.name for p in result.pruned] == ["hal-20261101T041700Z.db"]


# --- config ------------------------------------------------------------------


def test_the_retention_window_comes_from_config_not_the_code(tmp_path):
    (tmp_path / "config.toml").write_text(
        MINIMAL_TOML.replace("keep = 3", "keep = 42"), encoding="utf-8"
    )
    settings = load_settings(config_path=tmp_path / "config.toml", env={})

    assert settings.backup.keep == 42


def test_a_configured_backup_dir_is_anchored_to_config_toml(tmp_path):
    (tmp_path / "config.toml").write_text(
        MINIMAL_TOML + '\ndir = "snapshots"\n', encoding="utf-8"
    )
    settings = load_settings(
        config_path=tmp_path / "config.toml", env={"DB_PATH": "/srv/hal/hal.db"}
    )

    assert settings.backup_dir() == tmp_path / "snapshots"


def test_an_unconfigured_backup_dir_follows_the_database(tmp_path):
    (tmp_path / "config.toml").write_text(MINIMAL_TOML, encoding="utf-8")
    settings = load_settings(
        config_path=tmp_path / "config.toml", env={"DB_PATH": "/srv/hal/hal.db"}
    )

    assert settings.backup_dir() == Path("/srv/hal/backups")


def test_a_config_with_no_backup_section_still_loads(tmp_path):
    """Older config.toml files predate this section and must keep working."""
    (tmp_path / "config.toml").write_text(
        MINIMAL_TOML.replace("[backup]\nkeep = 3\n", ""), encoding="utf-8"
    )
    settings = load_settings(config_path=tmp_path / "config.toml", env={})

    assert settings.backup.keep > 0


def test_the_shipped_config_configures_backups():
    """The repo's own config.toml, not a fixture: this is the file on the box."""
    settings = load_settings(env={})

    assert settings.backup.keep >= 7, "a week is the minimum useful window"


# --- the CLI -----------------------------------------------------------------


def test_backup_is_a_subcommand(capsys):
    from hal_mary.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "backup" in capsys.readouterr().out


def test_the_backup_subcommand_prints_where_it_wrote(live_db, capsys, monkeypatch):
    from hal_mary import cli

    settings, _conn = live_db
    monkeypatch.setattr(cli, "load_cli_settings", lambda: settings)

    assert cli.main(["backup"]) == 0
    out = capsys.readouterr().out
    assert str(settings.db_path.parent / "backups") in out


def test_a_failed_backup_exits_nonzero_with_a_sentence(live_db, capsys, monkeypatch):
    from hal_mary import cli

    settings, _conn = live_db
    monkeypatch.setattr(cli, "load_cli_settings", lambda: settings)
    monkeypatch.setattr(
        cli, "run_backup", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only filesystem"))
    )

    assert cli.main(["backup"]) != 0
    assert "read-only filesystem" in capsys.readouterr().err
