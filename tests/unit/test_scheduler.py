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
        replace={
            'enabled = true\ncron = "0 8 * * tue"': 'enabled = false\ncron = "0 8 * * tue"'
        },
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


# --- the day each job actually fires ----------------------------------------
#
# The whole task turns on these. `CronTrigger.from_crontab` numbers weekdays
# from **Monday**, not from Sunday the way crontab(5) does, so `0 9 * * 0` — the
# obvious spelling of "Sunday morning" — fires on **Monday**, after every Sunday
# game has been played. Asserting the cron *string* renders somewhere catches
# none of that. These assert the computed fire time.

#: Which days each job is meant to run, by name. This is the intent; the cron
#: strings in config.toml are an implementation of it, and when the two disagree
#: it is the cron that is wrong.
INTENDED_DAYS = {
    "board_build": {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
    # First in the week, and again once the weekend's news has landed.
    "news_sweep": {"Wed", "Sat"},
    # Before ESPN processes the week's claims on Wednesday.
    "waiver_scan": {"Tue"},
    # Sunday morning, and again for the Thursday and Monday night games —
    # ESPN locks each player at his own kickoff, not once a week.
    "lineup_check": {"Sun", "Thu", "Mon"},
    "weekly_recap": {"Tue"},
}

#: A Monday noon UTC, so "the next fire" has somewhere to go in every direction.
FROM = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def fire_days(cron: str, zone, count: int = 14) -> list[str]:
    """The weekday of each of the next ``count`` fire times, as three letters."""
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(cron, timezone=zone)
    days, previous, now = [], None, FROM
    for _ in range(count):
        moment = trigger.get_next_fire_time(previous, now)
        days.append(moment.strftime("%a"))
        previous, now = moment, moment
    return days


def test_every_job_fires_on_the_day_it_is_meant_to(tmp_path):
    """The defect this exists for: `* * 0` means Monday to APScheduler."""
    settings = make_settings(tmp_path)
    zone = scheduler.scheduler_timezone(settings)

    for name, intended in INTENDED_DAYS.items():
        config = settings.job(name)
        assert config.crons, f"{name} has no cadence at all"
        fired = set()
        for cron in config.crons:
            days = fire_days(cron, zone)
            assert set(days) <= intended, (
                f"{name} cron {cron!r} fires on {sorted(set(days) - intended)}, "
                f"and is meant to run on {sorted(intended)}"
            )
            fired |= set(days)
        assert fired == intended, (
            f"{name} runs on {sorted(fired)} but is meant to run on {sorted(intended)}"
        )


def test_no_cadence_names_a_weekday_by_number(tmp_path):
    """A number in the day-of-week field is the bug, whatever number it is.

    APScheduler counts weekdays from Monday and crontab(5) counts from Sunday,
    so a digit there is right only by accident. Names are unambiguous in both.
    """
    settings = make_settings(tmp_path)
    for name, config in settings.jobs.items():
        for cron in config.crons:
            day_of_week = cron.split()[4]
            assert not any(char.isdigit() for char in day_of_week), (
                f"[jobs.{name}] cron {cron!r} names weekdays by number; "
                "use sun/mon/tue/wed/thu/fri/sat"
            )


def test_the_lineup_check_lands_before_sunday_kickoff_in_her_own_timezone(tmp_path):
    """UTC would put "Sunday morning" at 2am Pacific, before the inactive lists
    the prompt tells the model to go and read."""
    settings = make_settings(tmp_path)
    zone = scheduler.scheduler_timezone(settings)
    from apscheduler.triggers.cron import CronTrigger

    sundays = [
        CronTrigger.from_crontab(cron, timezone=zone).get_next_fire_time(None, FROM)
        for cron in settings.job("lineup_check").crons
    ]
    sunday = next(moment for moment in sundays if moment.strftime("%a") == "Sun")
    local = sunday.astimezone(zone)
    assert 6 <= local.hour <= 9, f"a Sunday check at {local:%H:%M %Z} is not a morning one"


def test_the_scheduler_runs_in_her_timezone_not_utc(tmp_path):
    settings = make_settings(tmp_path)
    assert "UTC" not in str(scheduler.scheduler_timezone(settings))


def test_a_job_with_several_cadences_gets_one_trigger_each(ready):
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=2))

    ids = job_ids(sched)
    assert "lineup_check" in ids
    extra = {job_id for job_id in ids if job_id.startswith("lineup_check#")}
    assert len(extra) == len(settings.job("lineup_check").crons) - 1
    for job in sched.get_jobs():
        if job.id.startswith("lineup_check"):
            assert job.max_instances == 1


def test_a_missed_fire_is_still_run_rather_than_skipped_in_silence(ready):
    """APScheduler's default grace is one second: a fire missed while the loop
    was blocked is dropped, and the only trace is a log line nobody reads."""
    _conn, settings, connect = ready
    sched = build(settings, connect, at(days=2))

    for job in sched.get_jobs():
        assert job.misfire_grace_time == settings.scheduler.misfire_grace_time_s
        assert job.misfire_grace_time > 60
