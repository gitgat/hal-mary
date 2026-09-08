"""Command line entry point.

``--help`` does no configuration, database or network work: a fresh checkout
with no ``.env`` has to be interrogable, and that is the first thing anyone runs
after a deploy. Every import that costs something happens inside the subcommand
that needs it.

The small ``load_cli_settings`` / ``open_db`` / ``build_client`` / ``run_*_sync``
indirections exist so the subcommands can be tested without a database,
credentials or ESPN. They are the seams, and they are deliberate.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from typing import Any

__all__ = ["build_parser", "main"]

DESCRIPTION = "hal-mary — a Claude-powered fantasy football advisor."

EPILOG = "Run `hal-mary job board_build` the day before a draft."

#: The environment keys ``sync`` cannot run without. ``WEB_PASSWORD`` is needed
#: to serve the web app but has nothing to do with reading ESPN.
ESPN_ENV_KEYS = ("ESPN_S2", "SWID", "LEAGUE_ID", "SEASON")

#: Exit codes. ``espn-check`` is meant for a monitoring script, so these are
#: part of its contract rather than decoration.
EXIT_OK = 0
EXIT_ESPN_FAILED = 1
EXIT_NOT_CONFIGURED = 2
#: A job that ran and failed. Same value as an ESPN failure on purpose: to a
#: shell script or a cron line, "it did not work" is one outcome.
EXIT_JOB_FAILED = 1
#: ``doctor`` found something fatal, or ``backup`` could not write. Both are read
#: by ``deploy/install.sh`` and ``deploy/deploy.sh``, which stop on nonzero.
EXIT_PREFLIGHT_FAILED = 1
EXIT_BACKUP_FAILED = 1


def load_cli_settings() -> Any:
    from hal_mary.config import load_settings

    return load_settings()


def open_db(settings: Any) -> sqlite3.Connection:
    from hal_mary import db

    conn = db.connect(settings.db_path)
    db.migrate(conn)
    return conn


def build_client(settings: Any) -> Any:
    from hal_mary.espn import EspnClient

    return EspnClient(settings)


def run_league_sync(conn: sqlite3.Connection, client: Any) -> dict[str, Any]:
    from hal_mary.espn import sync_league

    return sync_league(conn, client)


def run_draft_sync(conn: sqlite3.Connection, client: Any) -> list[dict[str, Any]]:
    from hal_mary.espn import sync_draft

    return sync_draft(conn, client)


def run_doctor_checks(settings: Any) -> Any:
    """The preflight, as a seam so the CLI can be tested without a box."""
    from hal_mary.doctor import run_checks

    return run_checks(settings)


def run_backup(settings: Any) -> Any:
    """The database snapshot, as a seam. See :mod:`hal_mary.backup`."""
    from hal_mary.backup import backup_database

    return backup_database(settings)


def build_runner(settings: Any, conn: sqlite3.Connection) -> Any:
    """The ``claude`` runner, as its own seam so a test never spawns one."""
    from hal_mary.claude_runner import ClaudeRunner

    return ClaudeRunner(settings, conn)

def refresh_action_plan(conn: sqlite3.Connection, settings: Any) -> None:
    """Recompute the Cowork action plan from what the sync just wrote.

    Named at module level so tests can replace it, and separate from the sync
    itself because it reads the database rather than ESPN.
    """
    from hal_mary.jobs.lineup_actions import refresh_after_sync

    refresh_after_sync(conn, settings)


def _missing_espn_config(settings: Any) -> list[str]:
    return [key for key in settings.missing_secrets() if key in ESPN_ENV_KEYS]


def _cmd_sync(_args: argparse.Namespace) -> int:
    from hal_mary.config import ConfigError
    from hal_mary.espn.client import EspnError

    settings = load_cli_settings()
    missing = _missing_espn_config(settings)
    if missing:
        print(
            f"cannot sync: {', '.join(missing)} not set. Put them in .env (see .env.example).",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    conn = open_db(settings)
    client = build_client(settings)
    try:
        summary = run_league_sync(conn, client)
        picks = run_draft_sync(conn, client)
        # After the writes, never before: the plan is arithmetic over the roster
        # and the week the sync just landed.
        refresh_action_plan(conn, settings)
    except ConfigError as exc:
        # Deliberately not folded in with the failures below. The sync itself
        # worked and its data is committed; what is broken is config.toml, and
        # the operator needs the key named rather than "sync failed".
        print(
            f"the sync wrote its data, but the action plan could not be built: {exc}",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED
    except (EspnError, sqlite3.Error, OSError) as exc:
        # Not just EspnError: a surprising payload can raise IntegrityError out
        # of the sync, and the operator running this from a terminal deserves
        # the same sentence for that as for an ESPN outage rather than a
        # traceback.
        print(f"sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ESPN_FAILED

    print(f"Synced {summary.get('league') or 'league'} ({settings.season}):")
    print(f"  teams        {summary.get('teams', 0)}")
    print(f"  players      {summary.get('players', 0)}")
    print(f"  roster slots {summary.get('roster_slots', 0)}")
    print(f"  free agents  {summary.get('free_agents', 0)}")

    if not picks:
        print("  draft        no new picks")
    else:
        print(f"  draft        {len(picks)} new pick(s):")
        for pick in picks:
            name = pick.get("player_name") or f"player {pick.get('player_id')}"
            print(f"    {pick.get('overall_pick')}. {name} -> team {pick.get('team_id')}")
    return EXIT_OK


def _cmd_espn_check(_args: argparse.Namespace) -> int:
    settings = load_cli_settings()
    ok, reason = build_client(settings).check_auth()
    if ok:
        print(reason)
        return EXIT_OK
    print(reason, file=sys.stderr)
    return EXIT_ESPN_FAILED


def build_optional_client(settings: Any) -> Any:
    """An ESPN client, or ``None`` when there is nothing to build one from.

    ``None`` rather than an error: every in-season job says what it can still do
    without ESPN, and hal-mary has to be runnable on a box with no cookies at
    all. A job that genuinely cannot work without it says so itself.
    """
    if _missing_espn_config(settings):
        return None
    try:
        return build_client(settings)
    except Exception as exc:  # noqa: BLE001 - a client we cannot build is one we do without
        print(f"warning: could not build an ESPN client ({exc}); running without it",
              file=sys.stderr)
        return None


def _cmd_job(args: argparse.Namespace) -> int:
    """Run one job on demand.

    The board is researched the day before the draft and takes minutes, so it
    needs a way to be started by hand — and to be startable again when it fails
    at six in the morning. In season it is the same command for the lineup check
    on a Sunday when the scheduler was asleep.

    A disabled job still runs from here. ``enabled = false`` keeps a job off the
    schedule; it does not mean "refuse to run it", and the one job somebody turns
    off is exactly the one they later need one more run of.
    """
    from hal_mary.jobs.registry import UnknownJob, run_job

    settings = load_cli_settings()
    conn = open_db(settings)
    try:
        outcome = run_job(
            args.name,
            conn,
            settings,
            build_runner(settings, conn),
            build_optional_client(settings),
        )
    except UnknownJob as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    if not outcome.ok:
        print(f"{args.name} failed: {outcome.error}", file=sys.stderr)
        return EXIT_JOB_FAILED
    print(f"{args.name}: {outcome.summary}")
    return EXIT_OK


def _cmd_jobs(_args: argparse.Namespace) -> int:
    """List what this build knows how to run, when, and how it went last time.

    The answer to "what does this thing actually do on its own?", which is the
    question anybody has after finding a process that has been running for two
    months.
    """
    from hal_mary.jobs.registry import all_names, get

    settings = load_cli_settings()
    conn = open_db(settings)

    rows = []
    for name in all_names():
        spec = get(name)
        config = settings.jobs.get(name)
        cadence = (config.cadence if config else "") or "on demand only"
        if config is not None and not config.enabled:
            cadence += " (turned off)"
        last = conn.execute(
            "SELECT started_at, status, summary, error FROM job_runs "
            "WHERE job = ? ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if last is None:
            outcome = "never run"
        else:
            detail = last["summary"] if last["status"] == "ok" else last["error"]
            outcome = f"{last['started_at'][:16]} {last['status']}: {detail or ''}".rstrip()
        rows.append((name, ", ".join(sorted(spec.phases)), cadence, outcome))

    width = max((len(row[0]) for row in rows), default=4)
    for name, phases, cadence, outcome in rows:
        print(f"{name.ljust(width)}  {cadence}")
        print(f"{' ' * width}  phase: {phases}")
        print(f"{' ' * width}  last:  {outcome}")
    return EXIT_OK


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Report whether this box could actually run hal-mary.

    Exits nonzero only for something fatal, so ``install.sh`` and ``deploy.sh``
    can gate on it while a missing memory directory still lets a deploy through.
    ``serve`` never runs this: see :mod:`hal_mary.doctor` for why the service
    boots degraded rather than refusing.
    """
    from hal_mary.config import ConfigError
    from hal_mary.doctor import render, worst_exit_code

    try:
        settings = load_cli_settings()
    except ConfigError as exc:
        # The command someone runs *because* config.toml is the problem may not
        # answer with a traceback.
        print(f"cannot run checks: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    checks = run_doctor_checks(settings)
    print(render(checks))
    return worst_exit_code(checks)


def _cmd_backup(_args: argparse.Namespace) -> int:
    """Snapshot the database. Run nightly by deploy/hal-mary-backup.timer."""
    settings = load_cli_settings()
    try:
        result = run_backup(settings)
    except (OSError, sqlite3.Error) as exc:
        print(f"backup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_BACKUP_FAILED

    print(f"Wrote {result.path} ({result.size_bytes:,} bytes)")
    if result.pruned:
        print(f"  pruned {len(result.pruned)} old backup(s): {result.pruned[0].name} ...")
    return EXIT_OK


def _cmd_migrate(_args: argparse.Namespace) -> int:
    """Apply pending schema migrations and say which ran.

    ``serve`` migrates on startup too, so this exists for ``deploy.sh``: a
    migration that fails should stop a deploy at the step named "migrate",
    before the tests and before the restart, rather than inside a service that
    then crash-loops.
    """
    from hal_mary import db

    settings = load_cli_settings()
    conn = db.connect(settings.db_path)
    try:
        applied = db.migrate(conn)
    except sqlite3.Error as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_JOB_FAILED
    finally:
        conn.close()

    if not applied:
        print(f"{settings.db_path}: schema already current")
    else:
        print(f"{settings.db_path}: applied {len(applied)} migration(s)")
        for name in applied:
            print(f"  {name}")
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = load_cli_settings()
    if not (settings.web_password or "").strip():
        # Refused, not warned. This binds every interface on the house network
        # and the database it serves holds live ESPN session cookies.
        print(
            "cannot serve: WEB_PASSWORD not set. The web app would put Caroline's "
            "ESPN session on the network with no password at all. Set WEB_PASSWORD "
            "in .env (see .env.example).",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    from hal_mary.web.serve import serve

    return serve(settings, reload=args.reload)


def _cmd_cowork_config(args: argparse.Namespace) -> int:
    """Print Cowork's scheduled tasks, rendered against this league.

    Not generic advice. The waiver run's time comes from the league's own
    processing day, because a claim submitted after the batch has run is worth
    nothing — and an assumed Wednesday is exactly the confident wrong answer
    that would go unnoticed for a season.
    """
    import json as _json

    from hal_mary import cowork

    settings = load_cli_settings()
    conn = open_db(settings)
    try:
        schedule = cowork.render(conn, settings)
    except cowork.CoworkConfigError as exc:
        print(f"cannot render the Cowork schedule: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(cowork.as_json(schedule), indent=2))
    else:
        print(cowork.render_text(schedule))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hal-mary", description=DESCRIPTION, epilog=EPILOG)
    subcommands = parser.add_subparsers(
        dest="command",
        metavar="{sync,espn-check,serve,job,jobs,cowork-config,doctor,migrate,backup}",
    )

    sync = subcommands.add_parser("sync", help="pull league state and draft picks from ESPN")
    sync.set_defaults(handler=_cmd_sync)

    check = subcommands.add_parser(
        "espn-check",
        help="report whether the ESPN cookies still work; exits nonzero when they do not",
    )
    check.set_defaults(handler=_cmd_espn_check)

    serve = subcommands.add_parser("serve", help="run the web app on the LAN")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="restart on code changes (development only)",
    )
    serve.set_defaults(handler=_cmd_serve)

    job = subcommands.add_parser("job", help="run one job now, by name")
    job.add_argument("name", help="which job to run, e.g. lineup_check")
    job.set_defaults(handler=_cmd_job)

    jobs = subcommands.add_parser(
        "jobs", help="list the jobs this build knows, with their cadence and last run"
    )
    jobs.set_defaults(handler=_cmd_jobs)

    cowork_config = subcommands.add_parser(
        "cowork-config",
        help="print Claude Cowork's scheduled tasks, rendered for this league",
    )
    cowork_config.add_argument(
        "--json",
        action="store_true",
        help="print the machine form instead of the paste-into-the-form one",
    )
    cowork_config.set_defaults(handler=_cmd_cowork_config)

    doctor = subcommands.add_parser(
        "doctor",
        help="check this box can actually run hal-mary; exits nonzero on a fatal problem",
    )
    doctor.set_defaults(handler=_cmd_doctor)

    migrate = subcommands.add_parser("migrate", help="apply pending database migrations")
    migrate.set_defaults(handler=_cmd_migrate)

    backup = subcommands.add_parser(
        "backup", help="snapshot the database and prune to the retention window"
    )
    backup.set_defaults(handler=_cmd_backup)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
