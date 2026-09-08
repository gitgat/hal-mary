"""Tests for the weekly recap: the job whose output is teaching, not reporting.

Over a season this is what turns "hal-mary told me to start him" into "I know why
he was the right start". So the assertions are about the lesson surviving: it has
to reach an ``advice`` row she reads *and* a note the rest of the season can
retrieve.
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
from hal_mary.jobs import weekly_recap
from hal_mary.jobs.registry import JobFailed, run_job

ANSWER = {
    "headline": "You won by 12, and the reason was the receiver nobody wanted.",
    "what_happened": "Ja'Marr Chase caught nine passes, which was nine points before he "
    "gained a single yard.",
    "what_it_means": "Your team is built on catches rather than touchdowns, which is "
    "steadier week to week.",
    "lesson": {
        "title": "Why catches matter more here than touchdowns",
        "explanation": "In this league every catch is worth a point on its own, so a "
        "player thrown to nine times has already scored nine points before yards. "
        "Touchdowns are worth more but happen far less predictably.",
    },
    "sources": ["https://example.com/box-score"],
}


def ready(tmp_path, results, *, roster=ROSTER):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_roster(conn, roster, team_id=6)
    return conn, settings, FakeRunner(settings, results), SeasonClient(roster=roster)


def test_the_recap_becomes_one_advice_row_she_can_read(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    weekly_recap.run(conn, settings, runner, client)

    rows = conn.execute("SELECT * FROM advice").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "recap"
    assert "nobody wanted" in rows[0]["headline"]
    assert "nine passes" in rows[0]["body"]


def test_the_lesson_is_in_the_body_where_she_will_read_it(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    weekly_recap.run(conn, settings, runner, client)

    body = conn.execute("SELECT body FROM advice").fetchone()["body"]
    assert "Why catches matter more here" in body
    assert "worth a point on its own" in body


def test_the_lesson_is_also_a_note_so_later_prompts_can_use_it(tmp_path):
    """A lesson she was taught in week 3 should not have to be re-derived in week 9."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    weekly_recap.run(conn, settings, runner, client)

    notes = memory.search_notes(conn, topics=["lesson"])
    assert notes and "catches" in notes[0]["text"]


def test_the_recap_reads_the_whole_season_not_just_three_weeks(tmp_path):
    """``build_context`` drops notes older than 21 days by default, which is
    exactly wrong for the job whose subject is the season so far."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])
    memory.write_note(
        conn,
        memory.Note(text="Something learned in the first week of the season.", source_job="t"),
    )
    conn.execute("UPDATE notes SET created_at = '2026-06-01T00:00:00+00:00'")
    conn.commit()

    weekly_recap.run(conn, settings, runner, client)

    assert "first week of the season" in (runner.calls[0]["extra_context"] or "")


def test_a_failed_call_writes_nothing(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    with pytest.raises(JobFailed):
        weekly_recap.run(conn, settings, runner, client)

    assert conn.execute("SELECT COUNT(*) AS n FROM advice").fetchone()["n"] == 0


def test_a_failure_is_recorded_on_the_job_run_row(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    outcome = run_job(weekly_recap.JOB_NAME, conn, settings, runner, client)

    assert outcome.ok is False
    assert conn.execute(
        "SELECT status FROM job_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()["status"] == "error"


def test_it_runs_with_web_tools_on(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.job(weekly_recap.JOB_NAME).tools
