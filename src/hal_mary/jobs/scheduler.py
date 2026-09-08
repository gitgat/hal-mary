"""What hal-mary runs, and when — the phase, and the APScheduler around it.

hal-mary's year has four phases, and which jobs exist at all depends on which one
it is in. A board build every morning is exactly right the week before the draft
and is a paid Claude call producing a board for a draft that already happened
every morning after it. A lineup check the week before the draft has no lineup to
check. So :func:`current_phase` decides, and :func:`build_scheduler` registers
only the jobs that belong to it.

The phase is re-evaluated daily on the scheduler's own clock, so a process
started the day before the draft becomes an in-season process by itself — rather
than at the next restart somebody remembers to do in October.

Three rules this module exists to hold:

* **A job never overlaps with itself.** ``max_instances=1`` and
  ``coalesce=True``. A research call can take fifteen minutes; a second copy
  starting on top of it is two ``claude`` subprocesses, two budgets, and two
  writers into one SQLite file.
* **Each run gets its own connection and its own runner, inside its own
  thread.** The web app's connection belongs to the event loop. See
  ``docs/DECISIONS.md``.
* **Nothing a job does can reach the scheduler.**
  :func:`hal_mary.jobs.registry.run_job` catches everything, so the listener here
  only has to log — and :func:`scheduled_run` still wraps the bookkeeping around
  it so that even a connection that will not open is a log line, not a dead
  scheduler.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from hal_mary.config import Settings
from hal_mary.jobs import registry

__all__ = [
    "PHASE_JOB_ID",
    "SchedulerContext",
    "apply_phase",
    "build_scheduler",
    "current_phase",
    "default_espn_client",
    "scheduled_run",
]

log = logging.getLogger(__name__)

#: The id of the job that re-evaluates the phase. Reserved: a research job may
#: not be called this, and the phase swap must never remove it.
PHASE_JOB_ID = "_phase_check"


# --- the phase ---------------------------------------------------------------


def current_phase(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> str:
    """Which of :data:`hal_mary.jobs.registry.PHASES` it is, right now.

    Derived from the league's draft date, with the windows configured under
    ``[scheduler]``. Before the draft (less its lead-in window) is ``pre_draft``;
    the window around it is ``draft_live``; the months after it are ``in_season``;
    beyond ``season_days`` is ``off_season``.

    **Never raises.** This runs on the way up, on a box that may have no league
    settings at all, and a process that will not start because it could not work
    out the date is a much worse failure than one that assumes the draft has not
    happened yet. With no draft date it falls back to the only other evidence
    there is: a real draft pick means the draft happened.
    """
    now = now or datetime.now(UTC)
    draft_at = _draft_datetime(conn, settings)

    if draft_at is None:
        return "in_season" if _has_made_picks(conn) else "pre_draft"

    config = settings.scheduler
    opens = draft_at - timedelta(hours=config.draft_window_before_hours)
    closes = draft_at + timedelta(hours=config.draft_window_after_hours)
    ends = draft_at + timedelta(days=config.season_days)

    if now < opens:
        return "pre_draft"
    if now <= closes:
        return "draft_live"
    if now <= ends:
        return "in_season"
    return "off_season"


def _draft_datetime(conn: sqlite3.Connection, settings: Settings) -> datetime | None:
    """The draft's start, as an aware UTC datetime, or ``None`` if unknowable."""
    from hal_mary.league import LeagueUnknown, load_league_context

    try:
        raw = load_league_context(conn, settings).draft_date
    except (LeagueUnknown, sqlite3.Error):
        return None
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        log.warning("league draft_date %r is not a timestamp; ignoring it", raw)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _has_made_picks(conn: sqlite3.Connection) -> bool:
    """Has anybody actually been drafted?

    The same rule as everywhere else: a row with no name and no positive player
    id is one of ESPN's pre-populated empty slots, not a pick. Counting one here
    would put a league that has not drafted yet into the season.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_picks "
            "WHERE player_name IS NOT NULL OR COALESCE(player_id, 0) > 0"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row["n"])


# --- the scheduler -----------------------------------------------------------


@dataclass(frozen=True)
class SchedulerContext:
    """Everything a scheduled run needs, carried on the scheduler itself.

    Attached as ``scheduler.hal_mary`` so :func:`apply_phase` can rebuild the job
    set later — at a daily phase check — without the caller having to hold on to
    five arguments for the life of the process.
    """

    settings: Settings
    connect: Callable[[], sqlite3.Connection]
    client_factory: Callable[[], Any] | None = None
    runner_factory: Callable[[Settings, sqlite3.Connection], Any] | None = None
    bus: Any = None


def build_scheduler(
    settings: Settings,
    *,
    connect: Callable[[], sqlite3.Connection],
    client_factory: Callable[[], Any] | None = None,
    runner_factory: Callable[[Settings, sqlite3.Connection], Any] | None = None,
    bus: Any = None,
    now: datetime | None = None,
) -> AsyncIOScheduler:
    """An ``AsyncIOScheduler`` carrying this phase's jobs, not yet started.

    ``connect`` is a factory, not a connection: every scheduled run opens its own
    inside its own worker thread. ``client_factory`` and ``runner_factory``
    default to real ones built per run, and are injected by the tests so nothing
    reaches ESPN or spawns ``claude``.

    Not started here. The caller starts it — ``web.serve`` does, inside the
    lifespan — so a process that fails to build one still serves pages.
    """
    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.hal_mary = SchedulerContext(  # type: ignore[attr-defined]
        settings=settings,
        connect=connect,
        client_factory=client_factory,
        runner_factory=runner_factory,
        bus=bus,
    )

    conn = connect()
    try:
        phase = current_phase(conn, settings, now)
    finally:
        conn.close()

    scheduler.add_job(
        _phase_check(scheduler),
        CronTrigger.from_crontab(settings.scheduler.phase_cron, timezone=UTC),
        id=PHASE_JOB_ID,
        name="re-check which phase of the season it is",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    apply_phase(scheduler, phase)
    return scheduler


def apply_phase(scheduler: AsyncIOScheduler, phase: str) -> list[str]:
    """Make the scheduler hold exactly this phase's enabled, cron'd jobs.

    Idempotent, and safe to call on a running scheduler: jobs no longer in the
    phase are removed, the ones that stay keep their next fire time, and new ones
    are added. :data:`PHASE_JOB_ID` is never touched — removing the job that
    re-checks the phase would freeze hal-mary in whichever phase it last saw.
    """
    context: SchedulerContext = scheduler.hal_mary  # type: ignore[attr-defined]
    settings = context.settings

    wanted: dict[str, str] = {}
    for spec in registry.specs_for_phase(phase):
        config = settings.jobs.get(spec.name)
        if config is None:
            log.warning("job %s has no [jobs.%s] section, so it cannot be scheduled",
                        spec.name, spec.name)
            continue
        if not config.enabled:
            log.info("job %s is disabled in config; not scheduling it", spec.name)
            continue
        if not config.cron:
            # Deliberate for the on-the-clock jobs: draft_advice has a config
            # entry and no cadence because the draft loop calls it directly.
            continue
        wanted[spec.name] = config.cron

    for job in list(scheduler.get_jobs()):
        if job.id != PHASE_JOB_ID and job.id not in wanted:
            scheduler.remove_job(job.id)

    for name, cron in wanted.items():
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=UTC)
        except ValueError:
            log.error("job %s has an unreadable cron %r; not scheduling it", name, cron)
            continue
        scheduler.add_job(
            scheduled_run(
                settings,
                connect=context.connect,
                name=name,
                client_factory=context.client_factory,
                runner_factory=context.runner_factory,
                bus=context.bus,
            ),
            trigger,
            id=name,
            name=registry.get(name).summary or name,
            # The two rules that keep one long research call from becoming two.
            # coalesce collapses a backlog (a laptop that was asleep) into one
            # run rather than firing every missed hour in a row.
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

    log.info("scheduler is in phase %s with jobs: %s", phase, ", ".join(sorted(wanted)) or "none")
    return sorted(wanted)


def _phase_check(scheduler: AsyncIOScheduler) -> Callable[[], None]:
    """The daily job that lets one process cross draft night by itself."""

    def check() -> None:
        context: SchedulerContext = scheduler.hal_mary  # type: ignore[attr-defined]
        try:
            conn = context.connect()
        except Exception:
            log.exception("could not open a connection to re-check the phase")
            return
        try:
            phase = current_phase(conn, context.settings)
        finally:
            conn.close()
        apply_phase(scheduler, phase)

    return check


# --- one scheduled run -------------------------------------------------------


def scheduled_run(
    settings: Settings,
    *,
    connect: Callable[[], sqlite3.Connection],
    name: str,
    client_factory: Callable[[], Any] | None = None,
    runner_factory: Callable[[Settings, sqlite3.Connection], Any] | None = None,
    bus: Any = None,
    close_connection: bool = True,
) -> Callable[[], None]:
    """The callable APScheduler fires: open a connection, run the job, close it.

    Runs on a worker thread. ``db.connect`` leaves ``check_same_thread`` on, so
    the connection is opened *here*, inside the thread that will use it — the
    same rule the draft loop follows.

    Nothing escapes. ``run_job`` already catches everything the job does; this
    catches everything around it, because a connection that will not open must be
    a log line rather than an exception surfacing inside APScheduler's executor.
    """

    def go() -> None:
        conn = None
        try:
            conn = connect()
            runner = (runner_factory or _default_runner)(settings, conn)
            client = client_factory() if client_factory is not None else default_espn_client(settings)
            outcome = registry.run_job(name, conn, settings, runner, client)
            _publish(bus, outcome)
        except Exception:
            log.exception("scheduled job %s could not be run at all", name)
        finally:
            if conn is not None and close_connection:
                try:
                    conn.close()
                except Exception:
                    log.exception("could not close the connection for job %s", name)

    return go


def _publish(bus: Any, outcome: registry.JobOutcome) -> None:
    """Tell the web app a job finished. Never the thing that breaks a job."""
    if bus is None:
        return
    try:
        bus.publish(
            "job",
            {"job": outcome.name, "ok": outcome.ok, "summary": outcome.summary or outcome.error},
        )
    except Exception:
        log.exception("could not publish the outcome of job %s", outcome.name)


def _default_runner(settings: Settings, conn: sqlite3.Connection) -> Any:
    from hal_mary.claude_runner import ClaudeRunner

    return ClaudeRunner(settings, conn)


def default_espn_client(settings: Settings) -> Any:
    """A real ESPN client, or ``None`` when there are no credentials to use one.

    ``None`` rather than an exception: hal-mary must be able to run with no ESPN
    at all, and the jobs each say what they can still do without it.
    """
    if not (settings.espn_s2 and settings.swid and settings.league_id):
        return None
    try:
        from hal_mary.espn import EspnClient

        return EspnClient(settings)
    except Exception:
        log.exception("could not build an ESPN client for a scheduled job")
        return None
