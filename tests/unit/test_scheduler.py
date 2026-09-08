"""Tests for the phase and the scheduler.

The scheduler runs inside the web process, so the properties that matter are
about isolation and about not doing more than was asked:

* **Only the jobs for this phase, and only the enabled ones.** A board build
  firing every morning in November is a paid Claude call that produces a draft
  board for a draft that happened in September.
* **Never two copies of one job.** A research job that runs long must not have a
  second copy start on top of it — that is two ``claude`` subprocesses, two
  budgets and two writers into SQLite.
* **Its own connection, inside its own worker.** The web app's connection lives
  on the event loop; a scheduled job runs on a thread.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from draft_fixtures import make_settings, open_db, seed_synced_league

from hal_mary.jobs import registry, scheduler

DRAFT = "2026-09-08T01:00:00+00:00"
DRAFT_AT = datetime.fromisoformat(DRAFT)


def at(**delta) -> datetime:
    return DRAFT_AT + timedelta(**delta)


@pytest.fixture
def ready(tmp_path):
    """A settings, a live connection, and a factory that opens fresh ones.

    The factory matters: everything that runs off the event loop opens its own
    connection inside its own worker, so a test that handed the scheduler one
    shared connection would be testing a shape production does not use.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, draft_date=DRAFT)
    return conn, settings, (lambda: open_db(tmp_path))


# --- the phase ---------------------------------------------------------------


def test_well_before_the_draft_is_pre_draft(ready):
    conn, settings, _connect = ready
    assert scheduler.current_phase(conn, settings, at(days=-3)) == "pre_draft"


def test_the_hours_around_the_draft_are_draft_live(ready):
    conn, settings, _connect = ready
    assert scheduler.current_phase(conn, settings, at(minutes=1)) == "draft_live"
    assert scheduler.current_phase(conn, settings, at(hours=2)) == "draft_live"


def test_the_window_opens_before_the_draft_starts(ready):
    """ESPN draft times slip, and being in fast mode early costs a few polls."""
    conn, settings, _connect = ready
    hours = settings.scheduler.draft_window_before_hours
    assert scheduler.current_phase(conn, settings, at(hours=-hours + 0.5)) == "draft_live"
    assert scheduler.current_phase(conn, settings, at(hours=-hours - 0.5)) == "pre_draft"


def test_the_day_after_the_draft_is_in_season(ready):
    conn, settings, _connect = ready
    assert scheduler.current_phase(conn, settings, at(days=2)) == "in_season"


def test_at_the_far_boundary_the_window_closes(ready):
    conn, settings, _connect = ready
    hours = settings.scheduler.draft_window_after_hours
    assert scheduler.current_phase(conn, settings, at(hours=hours - 0.5)) == "draft_live"
    assert scheduler.current_phase(conn, settings, at(hours=hours + 0.5)) == "in_season"


def test_long_after_the_draft_is_the_off_season(ready):
    conn, settings, _connect = ready
    days = settings.scheduler.season_days
    assert scheduler.current_phase(conn, settings, at(days=days - 1)) == "in_season"
    assert scheduler.current_phase(conn, settings, at(days=days + 1)) == "off_season"


def test_with_no_draft_date_and_no_picks_it_is_still_pre_draft(tmp_path):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, draft_date=None)
    assert scheduler.current_phase(conn, settings, datetime.now(UTC)) == "pre_draft"


def test_with_no_draft_date_but_picks_on_the_board_it_is_in_season(tmp_path):
    """ESPN can leave the draft date null. A made pick says the draft happened."""
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, draft_date=None)
    conn.execute(
        "INSERT INTO draft_picks (overall_pick, round_num, round_pick, team_id, "
        "player_id, player_name, seen_at) VALUES (1, 1, 1, 1, 42, 'Somebody', '2026-09-09')"
    )
    conn.commit()
    assert scheduler.current_phase(conn, settings, datetime.now(UTC)) == "in_season"


def test_an_unreadable_draft_date_does_not_raise(tmp_path):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, draft_date="soon-ish")
    assert scheduler.current_phase(conn, settings, datetime.now(UTC)) in registry.PHASES


def test_a_database_with_no_league_at_all_is_pre_draft(tmp_path):
    """A fresh box, before the first sync. It must not raise on the way up."""
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    assert scheduler.current_phase(conn, settings, datetime.now(UTC)) == "pre_draft"


# --- building the scheduler --------------------------------------------------


def build(settings, connect, now):
    return scheduler.build_scheduler(settings, connect=connect, now=now)


def job_ids(sched):
    return {job.id for job in sched.get_jobs()}


def test_only_the_jobs_for_this_phase_are_registered(ready):
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=-3))

    assert "board_build" in job_ids(sched)
    assert "lineup_check" not in job_ids(sched)

    sched = build(settings, connect, at(days=2))
    assert "board_build" not in job_ids(sched)
    assert {"lineup_check", "news_sweep", "waiver_scan"} <= job_ids(sched)


def test_a_job_turned_off_in_config_is_not_registered(tmp_path):
    settings = make_settings(
        tmp_path,
        # The cron is unique to this job, which makes the surrounding lines a
        # safe anchor for flipping exactly one `enabled`.
        replace={'enabled = true\ncron = "0 8 * * 2"': 'enabled = false\ncron = "0 8 * * 2"'},
    )
    conn = open_db(tmp_path)
    seed_synced_league(conn, draft_date=DRAFT)

    sched = build(settings, lambda: open_db(tmp_path), at(days=2))

    assert "waiver_scan" not in job_ids(sched)
    assert "lineup_check" in job_ids(sched)


def test_every_registered_job_refuses_to_overlap_with_itself(ready):
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=2))

    jobs = [job for job in sched.get_jobs() if job.id != scheduler.PHASE_JOB_ID]
    assert jobs
    for job in jobs:
        assert job.max_instances == 1, job.id
        assert job.coalesce is True, job.id


def test_the_phase_is_re_evaluated_on_its_own_schedule(ready):
    """So a process started before the draft becomes an in-season process by
    itself, rather than at the next restart somebody remembers to do."""
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=-3))
    assert scheduler.PHASE_JOB_ID in job_ids(sched)


def test_re_evaluating_the_phase_swaps_the_job_set(ready):
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=-3))
    assert "board_build" in job_ids(sched)

    scheduler.apply_phase(sched, "in_season")

    assert "board_build" not in job_ids(sched)
    assert "lineup_check" in job_ids(sched)
    assert scheduler.PHASE_JOB_ID in job_ids(sched), "the re-check must survive its own work"


def test_a_job_with_no_cron_is_not_scheduled(ready):
    """``draft_advice`` has a config entry and deliberately no cadence."""
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=2))
    assert "draft_advice" not in job_ids(sched)


# --- what a scheduled run actually does --------------------------------------


def test_a_scheduled_run_opens_its_own_connection_and_closes_it(ready, monkeypatch):
    _conn, settings, open_fresh = ready
    opened = []

    def connect():
        opened.append(open_fresh())
        return opened[-1]

    monkeypatch.setitem(
        registry.JOBS, "fake_sched", registry.JobSpec(
            name="fake_sched", run=lambda *_: "done", phases=frozenset({"in_season"})
        )
    )

    runner = scheduler.scheduled_run(
        settings, connect=connect, name="fake_sched", runner_factory=lambda *_: None
    )
    runner()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_a_scheduled_run_never_lets_a_failure_escape(ready, monkeypatch):
    """It is called by APScheduler inside the web process. Nothing may propagate."""
    conn, settings, _connect = ready

    def explode(*_):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        registry.JOBS, "fake_boom_sched", registry.JobSpec(
            name="fake_boom_sched", run=explode, phases=frozenset({"in_season"})
        )
    )

    scheduler.scheduled_run(
        settings,
        connect=lambda: conn,
        name="fake_boom_sched",
        runner_factory=lambda *_: None,
        close_connection=False,
    )()

    row = conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "error"


def test_a_scheduled_run_publishes_what_happened(ready, monkeypatch):
    conn, settings, _connect = ready
    published = []

    class Bus:
        def publish(self, event, payload):
            published.append((event, payload))

    monkeypatch.setitem(
        registry.JOBS, "fake_pub", registry.JobSpec(
            name="fake_pub", run=lambda *_: "all done", phases=frozenset({"in_season"})
        )
    )

    scheduler.scheduled_run(
        settings,
        connect=lambda: conn,
        name="fake_pub",
        runner_factory=lambda *_: None,
        bus=Bus(),
        close_connection=False,
    )()

    assert published == [("job", {"job": "fake_pub", "ok": True, "summary": "all done"})]


def test_a_bus_that_throws_does_not_break_the_job(ready, monkeypatch):
    conn, settings, _connect = ready

    class BadBus:
        def publish(self, event, payload):
            raise RuntimeError("nobody is listening")

    monkeypatch.setitem(
        registry.JOBS, "fake_badbus", registry.JobSpec(
            name="fake_badbus", run=lambda *_: "fine", phases=frozenset({"in_season"})
        )
    )

    scheduler.scheduled_run(
        settings,
        connect=lambda: conn,
        name="fake_badbus",
        runner_factory=lambda *_: None,
        bus=BadBus(),
        close_connection=False,
    )()

    assert conn.execute(
        "SELECT status FROM job_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()["status"] == "ok"
