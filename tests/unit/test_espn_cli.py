"""Tests for the ``sync`` and ``espn-check`` subcommands.

``espn-check`` is meant to be run from a monitoring script, so its exit code is
part of its contract, not decoration.
"""

from __future__ import annotations

import pytest

from conftest import FIXTURE_ENV
from hal_mary import cli, db
from hal_mary.config import load_settings
from hal_mary.espn.client import EspnUnavailable

SUMMARY = {
    "league": "The Gridiron Gauntlet",
    "teams": 4,
    "players": 9,
    "roster_slots": 6,
    "free_agents": 3,
}

PICKS = [
    {"overall_pick": 1, "round_num": 1, "round_pick": 1, "team_id": 3,
     "player_id": 4362628, "player_name": "Bijan Robinson"},
]


class FakeClient:
    def __init__(self, auth=(True, "ESPN credentials are valid")):
        self._auth = auth

    def check_auth(self):
        return self._auth


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the CLI at a scratch database and a client that never leaves the box."""
    settings = load_settings(env={**FIXTURE_ENV, "DB_PATH": str(tmp_path / "hal.db")})
    conn = db.connect(settings.db_path)
    db.migrate(conn)

    state = {"settings": settings, "conn": conn, "client": FakeClient()}
    monkeypatch.setattr(cli, "load_cli_settings", lambda: state["settings"])
    monkeypatch.setattr(cli, "open_db", lambda _settings: state["conn"])
    monkeypatch.setattr(cli, "build_client", lambda _settings: state["client"])
    monkeypatch.setattr(cli, "run_league_sync", lambda _conn, _client: SUMMARY)
    monkeypatch.setattr(cli, "run_draft_sync", lambda _conn, _client: PICKS)
    yield state
    conn.close()


# --- parser ----------------------------------------------------------------


def test_the_subcommands_are_documented(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    out = capsys.readouterr().out
    assert "sync" in out
    assert "espn-check" in out


def test_no_arguments_still_prints_usage(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


# --- sync ------------------------------------------------------------------


def test_sync_reports_what_it_wrote(wired, capsys):
    assert cli.main(["sync"]) == 0

    out = capsys.readouterr().out
    assert "The Gridiron Gauntlet" in out
    assert "4" in out
    assert "Bijan Robinson" in out


def test_sync_exits_nonzero_when_espn_fails(wired, monkeypatch, capsys):
    def explode(_conn, _client):
        raise EspnUnavailable("ESPN returned HTTP 503")

    monkeypatch.setattr(cli, "run_league_sync", explode)

    assert cli.main(["sync"]) == 1
    assert "503" in capsys.readouterr().err


def test_sync_refuses_to_start_without_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_cli_settings", lambda: load_settings(env={}))

    def unreachable(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("sync opened the database without credentials")

    monkeypatch.setattr(cli, "open_db", unreachable)

    assert cli.main(["sync"]) == 2
    err = capsys.readouterr().err
    assert "ESPN_S2" in err


# --- espn-check ------------------------------------------------------------


def test_espn_check_exits_zero_when_the_cookies_work(wired, capsys):
    assert cli.main(["espn-check"]) == 0
    assert "valid" in capsys.readouterr().out


def test_espn_check_exits_nonzero_when_the_cookies_are_stale(wired, capsys):
    wired["client"] = FakeClient(auth=(False, "ESPN credentials have expired"))

    assert cli.main(["espn-check"]) == 1
    assert "expired" in capsys.readouterr().err


def test_espn_check_does_not_need_the_database(monkeypatch, capsys):
    """A monitoring script must be able to run this on a box mid-deploy."""
    monkeypatch.setattr(cli, "load_cli_settings", lambda: load_settings(env=FIXTURE_ENV))
    monkeypatch.setattr(cli, "build_client", lambda _settings: FakeClient())

    def unreachable(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("espn-check opened the database")

    monkeypatch.setattr(cli, "open_db", unreachable)

    assert cli.main(["espn-check"]) == 0
