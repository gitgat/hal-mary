"""Tests for the ``sync`` and ``espn-check`` subcommands.

``espn-check`` is meant to be run from a monitoring script, so its exit code is
part of its contract, not decoration.
"""

from __future__ import annotations

import json
import sqlite3

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


def test_sync_reports_a_database_failure_readably(wired, monkeypatch, capsys):
    """An unexpected payload should not greet the operator with a traceback."""

    def explode(_conn, _client):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(cli, "run_league_sync", explode)

    assert cli.main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "FOREIGN KEY constraint failed" in err
    assert "Traceback" not in err


def test_sync_refreshes_the_action_plan(wired, monkeypatch, capsys):
    """A sync is the moment the roster and the week both change.

    That is exactly when a bye-week bench becomes true or stops being true, so
    it is where the deterministic producer runs — this application has no
    scheduler yet, and an action nobody ever produces is a loop that is not
    closed.
    """
    seen = []
    monkeypatch.setattr(
        cli, "refresh_action_plan", lambda conn, settings: seen.append((conn, settings))
    )

    assert cli.main(["sync"]) == 0
    assert len(seen) == 1
    assert seen[0][0] is wired["conn"]


def test_a_failing_action_plan_does_not_fail_the_sync(wired, capsys):
    """The roster, the free agents and the memory file are worth having alone."""

    def explode(_conn, _settings):
        raise RuntimeError("the board is missing")

    import hal_mary.jobs.lineup_actions as producer

    original = producer.emit_bye_week_benchings
    producer.emit_bye_week_benchings = explode
    try:
        assert cli.main(["sync"]) == 0
    finally:
        producer.emit_bye_week_benchings = original


# --- cowork-config ---------------------------------------------------------


def test_cowork_config_prints_the_schedule_for_this_league(wired, capsys):
    """Not generic advice: the waiver run's time comes from the league's own
    processing day, which is exactly the thing an assumed Wednesday gets wrong."""
    conn = wired["conn"]
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO league_settings
                (id, season, league_id, name, team_count, roster_slots_json, raw_json,
                 updated_at, current_week)
            VALUES (1, 2026, 7654321, 'The Invented League', 6, '{}', ?, ?, 5)
            """,
            (
                '{"acquisitionSettings": {"waiverProcessDays": ["THURSDAY"], "waiverHours": 3}}',
                "2026-10-01T12:00:00+00:00",
            ),
        )

    assert cli.main(["cowork-config"]) == 0

    out = capsys.readouterr().out
    assert "lineup-sunday" in out
    assert "waivers" in out
    assert "Wednesday" in out and "03:00" in out
    assert "Prompt (paste this whole block):" in out


def test_cowork_config_json_is_machine_readable(wired, capsys):
    assert cli.main(["cowork-config", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert {entry["name"] for entry in payload["tasks"]} >= {"lineup-sunday", "waivers"}
    assert "timezone" in payload


def test_cowork_config_reports_a_broken_task_file_rather_than_a_traceback(
    wired, monkeypatch, capsys, tmp_path
):
    broken = tmp_path / "tasks.toml"
    broken.write_text("[[task]]\nname = 'x'\ncadence = 'never'\n", encoding="utf-8")
    settings = wired["settings"]
    monkeypatch.setattr(
        cli,
        "load_cli_settings",
        lambda: settings.model_copy(
            # A Path, not a str: model_copy skips validation, and Settings hands
            # every consumer an anchored Path. A str here would test a shape
            # production never produces.
            update={"paths": settings.paths.model_copy(update={"cowork_tasks": broken})}
        ),
    )

    assert cli.main(["cowork-config"]) != 0
    assert "never" in capsys.readouterr().err
