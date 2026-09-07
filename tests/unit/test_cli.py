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
