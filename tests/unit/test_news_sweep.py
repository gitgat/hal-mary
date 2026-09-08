"""Tests for the news sweep: what changed about her players this week.

This is the job the other three read from. It writes notes and nothing else, so
the properties that matter are about retrieval: a note filed under a name the
lineup check will not search for is a note that never comes back.
"""

from __future__ import annotations

import pytest
from draft_fixtures import (
    FakeRunner,
    failed_result,
    make_settings,
    ok_result,
    open_db,
    seed_roster,
    seed_synced_league,
)
from test_lineup_check import ROSTER, SeasonClient

from hal_mary import memory
from hal_mary.jobs import news_sweep
from hal_mary.jobs.registry import JobFailed, run_job

FREE_AGENTS = [
    {"player_id": 201, "name": "Tank Bigsby", "position": "RB", "pro_team": "JAX",
     "percent_owned": 41.0},
    {"player_id": 202, "name": "Jauan Jennings", "position": "WR", "pro_team": "SF",
     "percent_owned": 33.0},
]

ANSWER = {
    "headline": "Two of her players changed status this week.",
    "notes": [
        {
            "player_name": "Ja'Marr Chase",
            "team_abbr": "CIN",
            "topic": "injury",
            "text": "Practised in full on Wednesday after a hamstring scare and is expected to play.",
            "source_url": "https://example.com/chase",
            "days_valid": 7,
        },
        {
            "player_name": "Tank Bigsby",
            "team_abbr": "JAX",
            "topic": "role",
            "text": "Took over the starting job after the man ahead of him was ruled out.",
            "source_url": "https://example.com/bigsby",
        },
        {"player_name": "Nobody", "text": "short", "topic": "news"},
    ],
}


def ready(tmp_path, results, *, roster=ROSTER, free_agents=FREE_AGENTS):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_roster(conn, roster, team_id=6)
    client = SeasonClient(roster=roster, free_agents=free_agents)
    return conn, settings, FakeRunner(settings, results), client


def test_it_writes_one_note_per_thing_it_learned(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    summary = news_sweep.run(conn, settings, runner, client)

    rows = conn.execute("SELECT * FROM notes ORDER BY id").fetchall()
    assert {row["player_name"] for row in rows} == {"Ja'Marr Chase", "Tank Bigsby"}
    assert all(row["source_job"] == news_sweep.JOB_NAME for row in rows)
    assert "2" in summary


def test_a_note_too_short_to_be_a_fact_is_dropped(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    news_sweep.run(conn, settings, runner, client)

    assert not conn.execute("SELECT 1 FROM notes WHERE text = 'short'").fetchall()


def test_the_notes_come_back_out_of_the_search_the_lineup_check_makes(tmp_path):
    """A note the next job cannot retrieve is a note that was never written."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    news_sweep.run(conn, settings, runner, client)

    found = memory.search_notes(conn, players=["Ja'Marr Chase"])
    assert found and "hamstring" in found[0]["text"]


def test_an_injury_note_is_given_a_shelf_life(tmp_path):
    """"He practised in full on Wednesday" is worth a lot now and misleading in
    three weeks, and Claude cannot tell a stale note from a fresh one."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    news_sweep.run(conn, settings, runner, client)

    rows = conn.execute("SELECT player_name, expires_at FROM notes").fetchall()
    assert all(row["expires_at"] for row in rows), dict(rows[0])


def test_the_prompt_carries_her_roster_and_the_free_agents(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    news_sweep.run(conn, settings, runner, client)

    prompt = runner.calls[0]["prompt"]
    assert "Ja'Marr Chase" in prompt
    assert "Tank Bigsby" in prompt


def test_it_runs_with_web_tools_on(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.job(news_sweep.JOB_NAME).tools


def test_a_failed_call_writes_nothing_and_says_so(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    with pytest.raises(JobFailed):
        news_sweep.run(conn, settings, runner, client)

    assert conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM advice").fetchone()["n"] == 0


def test_a_failure_is_recorded_on_the_job_run_row(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    outcome = run_job(news_sweep.JOB_NAME, conn, settings, runner, client)

    assert outcome.ok is False
    row = conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "error"
    assert row["error"]


def test_it_still_runs_when_espn_has_no_free_agents_to_give(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], free_agents=[])

    assert news_sweep.run(conn, settings, runner, client)


def test_an_empty_roster_is_a_clear_failure(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], roster=[])

    with pytest.raises(JobFailed):
        news_sweep.run(conn, settings, runner, client)
