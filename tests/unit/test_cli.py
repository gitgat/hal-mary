"""Tests for the CLI entry point.

The CLI is a stub in this task — later tasks add subcommands. What matters now is
that ``hal-mary --help`` works on a box with no ``.env`` and no database, because
that is the first thing anyone runs after a deploy.
"""

from pathlib import Path

import pytest

from hal_mary.cli import main

_UNIT_DIR = Path(__file__).resolve().parent


def test_no_arguments_prints_usage_and_succeeds(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "hal-mary" in out
    assert "usage" in out.lower()


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "hal-mary" in capsys.readouterr().out


def test_unknown_argument_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--not-a-flag"])
    assert excinfo.value.code != 0


def test_main_does_not_require_secrets(monkeypatch, capsys):
    """--help must work before .env exists; it must not touch config or the db."""
    for key in ("ESPN_S2", "SWID", "LEAGUE_ID", "TEAM_ID", "SEASON", "WEB_PASSWORD", "DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    assert main([]) == 0


# --- hal-mary job -----------------------------------------------------------


def stub_cli(monkeypatch, conn=None):
    """Point the CLI's seams at objects, so no test opens a database or ESPN."""
    from hal_mary import cli

    monkeypatch.setattr(cli, "load_cli_settings", lambda: object())
    monkeypatch.setattr(cli, "open_db", lambda settings: conn if conn is not None else object())
    monkeypatch.setattr(cli, "build_runner", lambda settings, conn: object())
    monkeypatch.setattr(cli, "build_optional_client", lambda settings: None)


def test_job_runs_the_named_job(monkeypatch, capsys):
    """The board is built from a terminal, the day before the draft."""
    from hal_mary.jobs import registry

    seen = {}

    def fake_board_build(conn, settings, runner, client):
        seen["ran"] = True
        return "200 players, 30 notes"

    monkeypatch.setitem(
        registry.JOBS,
        "board_build",
        registry.JobSpec(
            name="board_build", run=fake_board_build, phases=frozenset({"pre_draft"})
        ),
    )
    monkeypatch.setattr(registry, "run_job", _fake_run_job(registry, fake_board_build))
    stub_cli(monkeypatch)

    assert main(["job", "board_build"]) == 0
    assert seen["ran"] is True
    assert "200 players" in capsys.readouterr().out


def _fake_run_job(registry, func):
    """``run_job`` without the database, since these tests have no connection."""

    def run_job(name, conn, settings, runner=None, client=None):
        try:
            summary = func(conn, settings, runner, client)
        except registry.JobFailed as exc:
            return registry.JobOutcome(name=name, ok=False, error=str(exc))
        return registry.JobOutcome(name=name, ok=True, summary=summary)

    return run_job


def test_an_unknown_job_names_the_ones_that_exist(monkeypatch, capsys):
    stub_cli(monkeypatch)
    assert main(["job", "not-a-job"]) != 0
    err = capsys.readouterr().err
    assert "not-a-job" in err
    assert "lineup_check" in err, "tell the operator what they could have typed"


def test_a_failing_job_exits_nonzero(monkeypatch, capsys):
    """A job run from a terminal has to be usable from a shell script."""
    from hal_mary.jobs import registry

    def give_up(conn, settings, runner, client):
        raise registry.JobFailed("claude timed out")

    monkeypatch.setitem(
        registry.JOBS,
        "board_build",
        registry.JobSpec(name="board_build", run=give_up, phases=frozenset({"pre_draft"})),
    )
    monkeypatch.setattr(registry, "run_job", _fake_run_job(registry, give_up))
    stub_cli(monkeypatch)

    assert main(["job", "board_build"]) != 0
    assert "claude timed out" in capsys.readouterr().err


def test_job_appears_in_the_help(capsys):
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        main(["--help"])
    assert "job" in capsys.readouterr().out


# --- hal-mary jobs ----------------------------------------------------------


def test_jobs_lists_every_registered_job_with_its_cadence(monkeypatch, capsys, tmp_path):
    """The answer to "what does this thing actually do on its own?"."""
    import sys

    sys.path.insert(0, str(_UNIT_DIR))
    from draft_fixtures import make_settings, open_db

    from hal_mary import cli

    conn = open_db(tmp_path)
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "load_cli_settings", lambda: settings)
    monkeypatch.setattr(cli, "open_db", lambda _settings: conn)

    assert main(["jobs"]) == 0
    out = capsys.readouterr().out
    assert "lineup_check" in out
    assert "board_build" in out
    assert "0 9 * * 0" in out, "the cadence is the point of the listing"


def test_jobs_shows_when_each_one_last_ran(monkeypatch, capsys, tmp_path):
    import sys

    sys.path.insert(0, str(_UNIT_DIR))
    from draft_fixtures import make_settings, open_db

    from hal_mary import cli

    conn = open_db(tmp_path)
    conn.execute(
        "INSERT INTO job_runs (job, started_at, finished_at, status, summary) "
        "VALUES ('lineup_check', '2026-10-04T09:00:00+00:00', "
        "'2026-10-04T09:04:00+00:00', 'ok', 'week 5: 7 starters set')"
    )
    conn.commit()
    monkeypatch.setattr(cli, "load_cli_settings", lambda: make_settings(tmp_path))
    monkeypatch.setattr(cli, "open_db", lambda _settings: conn)

    assert main(["jobs"]) == 0
    assert "week 5: 7 starters set" in capsys.readouterr().out


def test_jobs_works_before_anything_has_ever_run(monkeypatch, capsys, tmp_path):
    import sys

    sys.path.insert(0, str(_UNIT_DIR))
    from draft_fixtures import make_settings, open_db

    from hal_mary import cli

    monkeypatch.setattr(cli, "load_cli_settings", lambda: make_settings(tmp_path))
    monkeypatch.setattr(cli, "open_db", lambda _settings: open_db(tmp_path))

    assert main(["jobs"]) == 0
    assert "never" in capsys.readouterr().out.lower()


# --- hal-mary migrate -------------------------------------------------------
#
# `serve` migrates on startup as well. This command exists so `deploy.sh` fails
# at the step called "migrate", with the SQL error in front of the person who
# ran it, rather than inside a restarted service that then crash-loops.


def test_migrate_applies_and_names_the_migrations(tmp_path, monkeypatch, capsys):
    from hal_mary import cli
    from hal_mary.config import load_settings

    settings = load_settings(env={"DB_PATH": str(tmp_path / "hal.db")})
    monkeypatch.setattr(cli, "load_cli_settings", lambda: settings)

    assert main(["migrate"]) == 0

    out = capsys.readouterr().out
    assert "001_initial.sql" in out
    assert str(tmp_path / "hal.db") in out


def test_migrate_is_idempotent_and_says_so(tmp_path, monkeypatch, capsys):
    from hal_mary import cli
    from hal_mary.config import load_settings

    settings = load_settings(env={"DB_PATH": str(tmp_path / "hal.db")})
    monkeypatch.setattr(cli, "load_cli_settings", lambda: settings)

    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert main(["migrate"]) == 0
    assert "already current" in capsys.readouterr().out


def test_a_failing_migration_exits_nonzero(tmp_path, monkeypatch, capsys):
    """A red migration must stop a deploy before it restarts anything."""
    import sqlite3

    from hal_mary import cli, db
    from hal_mary.config import load_settings

    settings = load_settings(env={"DB_PATH": str(tmp_path / "hal.db")})
    monkeypatch.setattr(cli, "load_cli_settings", lambda: settings)
    monkeypatch.setattr(
        db, "migrate", lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("near AS"))
    )

    assert main(["migrate"]) != 0
    assert "near AS" in capsys.readouterr().err
