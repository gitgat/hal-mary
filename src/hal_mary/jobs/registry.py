"""One table of jobs, and the one wrapper that runs them.

Every job in hal-mary — the pre-draft board build, the four in-season research
jobs — has the same signature::

    run(conn, settings, runner, client) -> str

and returns one line saying what it did. That uniformity is the whole point: the
scheduler, ``hal-mary job``, and the "run it now" button all treat jobs
interchangeably because there is nothing to treat differently.

Two rules this module exists to enforce.

**A failing job must never take the process down.** hal-mary running with a
broken waiver scan is worth a great deal; hal-mary not running is worth nothing.
:func:`run_job` catches every exception — including the ones that are not
``Exception`` — records it on the ``job_runs`` row, and returns an outcome. It
does not re-raise, and neither does its own bookkeeping: a database that cannot
record the run is not allowed to be the thing that stops the run.

**An unknown job name is a sentence.** ``hal-mary job lineup`` is a plausible
typo, and the answer to it is the list of names that would have worked, not a
``KeyError`` traceback.

The job modules are imported lazily, on first lookup. That keeps ``--help`` free
of every import in the application, and it is why the job modules can import
:func:`register` from here without a circular import.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from hal_mary import db

__all__ = [
    "JOBS",
    "PHASES",
    "JobFailed",
    "JobOutcome",
    "JobSpec",
    "UnknownJob",
    "all_names",
    "get",
    "job_names",
    "register",
    "run_job",
    "specs_for_phase",
]

log = logging.getLogger(__name__)

#: The phases of hal-mary's year. A job declares which of them it belongs to and
#: the scheduler registers it only while that phase is current, so the process
#: moves from a nightly board build to a weekly lineup check on its own.
PHASES = ("pre_draft", "draft_live", "in_season", "off_season")

#: Modules that register jobs. Imported on first lookup rather than at import
#: time: ``hal-mary --help`` has to work on a fresh checkout with no database and
#: no credentials, and every one of these pulls in the world.
JOB_MODULES = (
    "hal_mary.jobs.board_build",
    "hal_mary.jobs.news_sweep",
    "hal_mary.jobs.waiver_scan",
    "hal_mary.jobs.lineup_check",
    "hal_mary.jobs.weekly_recap",
)

#: What is recorded when a job worked but had nothing to say. Never empty: the
#: status page renders the summary, and a blank one reads as "it never ran".
DID_NOTHING = "ran, with nothing to report"

#: The two that genuinely mean "stop". Cancellation is the scheduler shutting
#: down, and swallowing it would make a job impossible to stop — the opposite of
#: the failure this module defends against.
_FATAL = (KeyboardInterrupt, asyncio.CancelledError)


class UnknownJob(ValueError):
    """A name nobody registered. Its message carries the names that would work."""


class JobFailed(RuntimeError):
    """A job saying "I could not do my work", with the reason in plain English.

    Distinct from an unexpected exception only in that its message is written
    for whoever is reading the status page rather than for a developer reading a
    traceback. :func:`run_job` records both the same way.
    """


JobCallable = Callable[[sqlite3.Connection, Any, Any, Any], str | None]


@dataclass(frozen=True)
class JobSpec:
    """One job: what to call, and when it is allowed to run."""

    name: str
    run: JobCallable
    phases: frozenset[str]
    #: One line for ``hal-mary jobs`` and the status page, saying what it does.
    summary: str = ""


@dataclass(frozen=True)
class JobOutcome:
    """What happened, in the shape the CLI and the web app both render.

    ``run_id`` is ``None`` when the bookkeeping itself failed — the job may have
    run and worked anyway, which is why it is separate from ``ok``.
    """

    name: str
    ok: bool
    summary: str | None = None
    error: str | None = None
    run_id: int | None = None


JOBS: dict[str, JobSpec] = {}

_loaded = False


def register(
    name: str, *, phases: Iterable[str], summary: str = ""
) -> Callable[[JobCallable], JobCallable]:
    """Decorator: put a job in the table under ``name``.

    ``phases`` is required and is checked against :data:`PHASES`. A job naming a
    phase nobody schedules would simply never run, and would never say so — the
    most expensive kind of silence.
    """
    wanted = frozenset(phases)
    unknown = sorted(wanted - set(PHASES))
    if unknown:
        raise ValueError(
            f"job {name!r} names phases nobody schedules: {', '.join(unknown)}. "
            f"The phases are {', '.join(PHASES)}."
        )
    if not wanted:
        raise ValueError(f"job {name!r} runs in no phase, so nothing would ever schedule it")

    def decorate(func: JobCallable) -> JobCallable:
        if name in JOBS:
            raise ValueError(
                f"two jobs are registered as {name!r}. A job name is also a "
                "``[jobs.*]`` config key and a ``job_runs.job`` value, so it has "
                "to be unique."
            )
        JOBS[name] = JobSpec(name=name, run=func, phases=wanted, summary=summary)
        return func

    return decorate


def _ensure_loaded() -> None:
    """Import the job modules once, so their decorators have run."""
    global _loaded
    if _loaded:
        return
    # Set before importing, not after: a job module that reaches back into the
    # registry mid-import must not send us round again.
    _loaded = True
    for module in JOB_MODULES:
        importlib.import_module(module)


def all_names() -> list[str]:
    """Every registered job name, sorted."""
    _ensure_loaded()
    return sorted(JOBS)


def job_names() -> str:
    """The registered names as one comma-separated string, for a message."""
    return ", ".join(all_names())


def get(name: str) -> JobSpec:
    """The spec for ``name``. Raises :class:`UnknownJob` naming the real ones."""
    _ensure_loaded()
    spec = JOBS.get(name)
    if spec is None:
        raise UnknownJob(f"unknown job {name!r}. This build knows: {job_names()}.")
    return spec


def specs_for_phase(phase: str) -> list[JobSpec]:
    """Every job that belongs to ``phase``, in name order."""
    return [spec for spec in (get(name) for name in all_names()) if phase in spec.phases]


def run_job(
    name: str,
    conn: sqlite3.Connection,
    settings: Any,
    runner: Any = None,
    client: Any = None,
) -> JobOutcome:
    """Run one job, record it, and never let it escape.

    Raises :class:`UnknownJob` for a name nobody registered — and only for that.
    It happens *before* a ``job_runs`` row is opened, so a typo never leaves a
    run behind that looks like it started and never finished.

    Everything the job itself does wrong comes back as ``ok=False`` with the
    reason on the outcome and on the ``job_runs`` row. Nothing propagates: this
    is called from a scheduler thread inside the web process, and a job that
    could take that process down would take the draft page with it.

    A disabled job still runs when it is asked for by name. ``enabled = false``
    means "keep this off the schedule", not "refuse to run it" — the button on
    the status page and ``hal-mary job`` exist precisely for the job that has
    been turned off and needs one more run.
    """
    spec = get(name)
    run_id = _start_run(conn, name)

    try:
        summary = spec.run(conn, settings, runner, client)
    except JobFailed as exc:
        log.warning("job %s could not finish: %s", name, exc)
        _finish_run(conn, run_id, "error", error=str(exc))
        return JobOutcome(name=name, ok=False, error=str(exc), run_id=run_id)
    except _FATAL as exc:
        _finish_run(conn, run_id, "error", error=f"{type(exc).__name__}: cancelled")
        raise
    except BaseException as exc:
        log.exception("job %s raised", name)
        message = f"{type(exc).__name__}: {exc}"
        _finish_run(conn, run_id, "error", error=message)
        return JobOutcome(name=name, ok=False, error=message, run_id=run_id)

    text = (summary or "").strip() or DID_NOTHING
    _finish_run(conn, run_id, "ok", summary=text)
    log.info("job %s: %s", name, text)
    return JobOutcome(name=name, ok=True, summary=text, run_id=run_id)


def _start_run(conn: sqlite3.Connection, name: str) -> int | None:
    try:
        return db.job_run_started(conn, name)
    except Exception:
        log.exception("could not open a job_runs row for %s; running it anyway", name)
        return None


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int | None,
    status: str,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    if run_id is None:
        return
    try:
        db.job_run_finished(conn, run_id, status, summary=summary, error=error)
    except Exception:
        log.exception("could not close the job_runs row for run %s", run_id)
