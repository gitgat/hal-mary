"""Tests for the job registry and ``run_job``.

Two properties matter more than anything else here:

* **A failing job never takes the process down.** hal-mary running with one
  broken job is vastly better than hal-mary not running, so ``run_job`` catches
  everything, writes the error to ``job_runs``, and returns.
* **An unknown name is a sentence, not a traceback.** ``hal-mary job lineup``
  is a typo somebody makes at 8am on a Sunday, and the answer has to be the list
  of names that would have worked.
"""

from __future__ import annotations

import sqlite3

import pytest
from draft_fixtures import make_settings, open_db, seed_synced_league

from hal_mary.jobs import registry


@pytest.fixture
def ready(tmp_path):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    return conn, settings


def last_run(conn):
    return conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()


# --- the table ---------------------------------------------------------------


def test_every_registered_job_names_a_phase_the_scheduler_knows():
    for name in registry.all_names():
        spec = registry.get(name)
        assert spec.phases, f"{name} runs in no phase, so nothing would ever schedule it"
        assert spec.phases <= set(registry.PHASES), f"{name} names a phase that does not exist"


def test_the_four_in_season_jobs_and_the_board_build_are_all_registered():
    assert set(registry.all_names()) >= {
        "board_build",
        "news_sweep",
        "waiver_scan",
        "lineup_check",
        "weekly_recap",
    }


def test_every_registered_job_has_a_config_section(tmp_path):
    """A job with no ``[jobs.*]`` entry has no model, no timeout and no cadence."""
    settings = make_settings(tmp_path)
    for name in registry.all_names():
        assert settings.job(name) is not None, f"config.toml has no [jobs.{name}]"


def test_the_board_build_is_a_pre_draft_job_and_the_others_are_in_season():
    assert registry.get("board_build").phases == {"pre_draft"}
    for name in ("news_sweep", "waiver_scan", "lineup_check", "weekly_recap"):
        assert registry.get(name).phases == {"in_season"}, name


def test_an_unknown_name_raises_something_that_lists_the_real_ones():
    with pytest.raises(registry.UnknownJob) as caught:
        registry.get("lineup")
    message = str(caught.value)
    assert "lineup" in message
    assert "lineup_check" in message, "the message has to show what she meant to type"


def test_registering_the_same_name_twice_is_refused():
    with pytest.raises(ValueError):

        @registry.register("board_build", phases=("pre_draft",))
        def _duplicate(conn, settings, runner, client):  # pragma: no cover
            return ""


# --- run_job -----------------------------------------------------------------


def test_run_job_records_a_successful_run_with_its_summary(ready, monkeypatch):
    conn, settings = ready
    monkeypatch.setitem(registry.JOBS, "fake_ok", _spec(lambda *_: "did the thing"))

    outcome = registry.run_job("fake_ok", conn, settings, runner=None, client=None)

    assert outcome.ok is True
    assert outcome.summary == "did the thing"
    row = last_run(conn)
    assert (row["job"], row["status"], row["summary"]) == ("fake_ok", "ok", "did the thing")
    assert row["finished_at"], "an unfinished row reads as a job still running, forever"


def test_run_job_swallows_an_exception_and_records_it(ready, monkeypatch):
    conn, settings = ready

    def explode(*_):
        raise RuntimeError("ESPN fell over")

    monkeypatch.setitem(registry.JOBS, "fake_boom", _spec(explode))

    outcome = registry.run_job("fake_boom", conn, settings, runner=None, client=None)

    assert outcome.ok is False
    assert "ESPN fell over" in (outcome.error or "")
    row = last_run(conn)
    assert row["status"] == "error"
    assert "ESPN fell over" in (row["error"] or "")


def test_run_job_swallows_even_a_baseexception_that_is_not_an_exception(ready, monkeypatch):
    """A job that raises ``KeyboardInterrupt`` is still not allowed to kill serve."""
    conn, settings = ready

    def explode(*_):
        raise MemoryError("out of memory building the prompt")

    monkeypatch.setitem(registry.JOBS, "fake_oom", _spec(explode))

    outcome = registry.run_job("fake_oom", conn, settings, runner=None, client=None)

    assert outcome.ok is False
    assert last_run(conn)["status"] == "error"


def test_a_job_that_reports_failure_without_raising_is_recorded_as_an_error(ready, monkeypatch):
    conn, settings = ready

    def give_up(*_):
        raise registry.JobFailed("the research call came back with nothing usable")

    monkeypatch.setitem(registry.JOBS, "fake_giveup", _spec(give_up))

    outcome = registry.run_job("fake_giveup", conn, settings, runner=None, client=None)

    assert outcome.ok is False
    assert outcome.error == "the research call came back with nothing usable"
    assert last_run(conn)["status"] == "error"


def test_a_job_that_returns_nothing_still_gets_a_summary(ready, monkeypatch):
    """``summary`` is what the status page renders. Empty reads as "never ran"."""
    conn, settings = ready
    monkeypatch.setitem(registry.JOBS, "fake_quiet", _spec(lambda *_: None))

    outcome = registry.run_job("fake_quiet", conn, settings, runner=None, client=None)

    assert outcome.ok is True
    assert outcome.summary
    assert last_run(conn)["summary"] == outcome.summary


def test_run_job_on_an_unknown_name_raises_before_it_writes_a_run_row(ready):
    conn, settings = ready
    with pytest.raises(registry.UnknownJob):
        registry.run_job("nope", conn, settings, runner=None, client=None)
    assert last_run(conn) is None, "a name that never ran must not appear to have run"


def test_run_job_passes_the_runner_and_the_client_through(ready, monkeypatch):
    conn, settings = ready
    seen = {}

    def record(conn_, settings_, runner_, client_):
        seen.update(conn=conn_, settings=settings_, runner=runner_, client=client_)
        return "ok"

    monkeypatch.setitem(registry.JOBS, "fake_args", _spec(record))
    registry.run_job("fake_args", conn, settings, runner="RUNNER", client="CLIENT")

    assert seen == {"conn": conn, "settings": settings, "runner": "RUNNER", "client": "CLIENT"}


def test_run_job_survives_a_database_that_cannot_even_record_the_failure(ready, monkeypatch):
    """The last line of defence: bookkeeping that fails must not raise either."""
    conn, settings = ready
    monkeypatch.setitem(registry.JOBS, "fake_ok2", _spec(lambda *_: "fine"))
    monkeypatch.setattr(
        registry.db, "job_run_started", _raiser("no such table: job_runs")
    )

    outcome = registry.run_job("fake_ok2", conn, settings, runner=None, client=None)

    assert outcome.ok is True, "the job itself worked; only the bookkeeping did not"
    assert outcome.run_id is None


def test_run_job_records_exactly_one_row_for_the_board_build(ready):
    """The board build used to open its own ``job_runs`` row. Two rows for one
    run makes the status page report a job that ran twice and finished once."""
    conn, settings = ready

    class DeadRunner:
        def run(self, *args, **kwargs):
            raise RuntimeError("no claude here")

    registry.run_job("board_build", conn, settings, runner=DeadRunner(), client=None)

    rows = conn.execute("SELECT * FROM job_runs WHERE job = 'board_build'").fetchall()
    assert len(rows) == 1


# --- helpers -----------------------------------------------------------------


def _spec(func):
    return registry.JobSpec(name="fake", run=func, phases=frozenset({"in_season"}), summary="")


def _raiser(message):
    def raise_it(*_args, **_kwargs):
        raise sqlite3.OperationalError(message)

    return raise_it
