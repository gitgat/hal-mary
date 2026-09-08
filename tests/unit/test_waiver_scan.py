"""Tests for the waiver scan: who to claim, who to drop, and by when.

The deadline is not decoration. A claim she reads about on Wednesday and acts on
on Thursday is a claim somebody else already made, so every row this job writes
has to say when it stops being possible.
"""

from __future__ import annotations

import json

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
from test_news_sweep import FREE_AGENTS

from hal_mary.jobs import waiver_scan
from hal_mary.jobs.registry import JobFailed, run_job

ANSWER = {
    "deadline": "Wednesday morning, when ESPN processes this week's claims",
    "headline": "One claim worth making this week.",
    "claims": [
        {
            "add": "Tank Bigsby",
            "position": "RB",
            "drop": "Trey McBride",
            "urgency": "high",
            "bid": "Use most of your budget — he is the best player available.",
            "reason": "The man ahead of him is out for the season, so he gets the ball now.",
            "source_url": "https://example.com/bigsby",
        },
        {
            "add": "Jauan Jennings",
            "position": "WR",
            "drop": None,
            "urgency": "low",
            "bid": "A small bid only.",
            "reason": "He catches a lot of short passes, which is worth points here.",
            "source_url": "https://example.com/jennings",
        },
    ],
}


def ready(tmp_path, results, *, roster=ROSTER, free_agents=FREE_AGENTS):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_roster(conn, roster, team_id=6)
    client = SeasonClient(roster=roster, free_agents=free_agents)
    return conn, settings, FakeRunner(settings, results), client


def advice_rows(conn):
    return conn.execute("SELECT * FROM advice ORDER BY id").fetchall()


def test_each_claim_becomes_its_own_advice_row_she_can_tick_off(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    waiver_scan.run(conn, settings, runner, client)

    rows = advice_rows(conn)
    assert len(rows) == 2
    assert all(row["kind"] == "waiver" for row in rows)
    assert all(row["done"] == 0 for row in rows)
    assert "Tank Bigsby" in rows[0]["headline"]


def test_every_row_states_the_deadline(tmp_path):
    """She reads these on a phone, one card at a time. A deadline that is only on
    the first card is a deadline she does not see."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    waiver_scan.run(conn, settings, runner, client)

    for row in advice_rows(conn):
        assert "Wednesday morning" in (row["body"] or ""), row["headline"]


def test_a_claim_that_needs_a_drop_says_who_to_drop(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    waiver_scan.run(conn, settings, runner, client)

    row = advice_rows(conn)[0]
    assert "Trey McBride" in row["body"]
    assert json.loads(row["payload_json"])["drop"] == "Trey McBride"


def test_a_claim_naming_a_player_she_does_not_own_is_dropped(tmp_path):
    """Telling her to drop somebody who is not on her team is an instruction she
    cannot follow, and it makes every other row less trustworthy."""
    answer = dict(ANSWER, claims=[dict(ANSWER["claims"][0], drop="Somebody Else")])
    conn, settings, runner, client = ready(tmp_path, [ok_result(answer)])

    waiver_scan.run(conn, settings, runner, client)

    rows = advice_rows(conn)
    assert len(rows) == 1
    assert "Somebody Else" not in rows[0]["body"]
    assert json.loads(rows[0]["payload_json"])["drop"] is None


def test_the_summary_names_the_top_claim(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    summary = waiver_scan.run(conn, settings, runner, client)

    assert "Tank Bigsby" in summary


def test_the_prompt_carries_the_wire_and_her_roster(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    waiver_scan.run(conn, settings, runner, client)

    prompt = runner.calls[0]["prompt"]
    assert "Tank Bigsby" in prompt
    assert "Ja'Marr Chase" in prompt


def test_it_runs_with_web_tools_on(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.job(waiver_scan.JOB_NAME).tools


def test_a_failed_call_writes_no_partial_advice(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    with pytest.raises(JobFailed):
        waiver_scan.run(conn, settings, runner, client)

    assert advice_rows(conn) == []


def test_a_failure_is_recorded_on_the_job_run_row(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    outcome = run_job(waiver_scan.JOB_NAME, conn, settings, runner, client)

    assert outcome.ok is False
    assert conn.execute("SELECT status FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()[
        "status"
    ] == "error"


def test_no_free_agents_at_all_is_a_clear_failure_not_an_empty_card(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], free_agents=[])

    with pytest.raises(JobFailed):
        waiver_scan.run(conn, settings, runner, client)

    assert runner.calls == [], "no point paying for a scan of an empty wire"


def test_a_run_with_nothing_worth_claiming_says_so_and_writes_nothing(tmp_path):
    """"Do nothing this week" is a real answer and must not look like a failure."""
    conn, settings, runner, client = ready(
        tmp_path, [ok_result({"deadline": "Wednesday", "claims": []})]
    )

    summary = waiver_scan.run(conn, settings, runner, client)

    assert "nothing" in summary.lower()
    assert advice_rows(conn) == []
