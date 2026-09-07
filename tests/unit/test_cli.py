"""Tests for the CLI entry point.

The CLI is a stub in this task — later tasks add subcommands. What matters now is
that ``hal-mary --help`` works on a box with no ``.env`` and no database, because
that is the first thing anyone runs after a deploy.
"""

import pytest

from hal_mary.cli import main


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


def test_job_runs_the_named_job(monkeypatch, capsys):
    """The board is built from a terminal, the day before the draft."""
    from hal_mary import cli
    from hal_mary.jobs import registry

    seen = {}

    def fake_board_build(conn, settings, runner):
        seen["ran"] = True
        return {"ok": True, "players": 200, "notes": 30, "summary": "200 players, 30 notes"}

    monkeypatch.setitem(registry.JOBS, "board_build", fake_board_build)
    monkeypatch.setattr(cli, "load_cli_settings", lambda: object())
    monkeypatch.setattr(cli, "open_db", lambda settings: object())
    monkeypatch.setattr(cli, "build_runner", lambda settings, conn: object())

    assert main(["job", "board_build"]) == 0
    assert seen["ran"] is True
    assert "200 players" in capsys.readouterr().out


def test_an_unknown_job_names_the_ones_that_exist(monkeypatch, capsys):
    from hal_mary import cli

    monkeypatch.setattr(cli, "load_cli_settings", lambda: object())
    assert main(["job", "not-a-job"]) != 0
    err = capsys.readouterr().err
    assert "not-a-job" in err
    assert "board_build" in err, "tell the operator what they could have typed"


def test_a_failing_job_exits_nonzero(monkeypatch, capsys):
    """A job run from a terminal has to be usable from a shell script."""
    from hal_mary import cli
    from hal_mary.jobs import registry

    monkeypatch.setitem(
        registry.JOBS,
        "board_build",
        lambda conn, settings, runner: {"ok": False, "error": "claude timed out"},
    )
    monkeypatch.setattr(cli, "load_cli_settings", lambda: object())
    monkeypatch.setattr(cli, "open_db", lambda settings: object())
    monkeypatch.setattr(cli, "build_runner", lambda settings, conn: object())

    assert main(["job", "board_build"]) != 0
    assert "claude timed out" in capsys.readouterr().err


def test_job_appears_in_the_help(capsys):
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        main(["--help"])
    assert "job" in capsys.readouterr().out
