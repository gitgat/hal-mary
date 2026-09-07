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


def build_runner(settings: Any, conn: sqlite3.Connection) -> Any:
    """The ``claude`` runner, as its own seam so a test never spawns one."""
    from hal_mary.claude_runner import ClaudeRunner

    return ClaudeRunner(settings, conn)


def _missing_espn_config(settings: Any) -> list[str]:
    return [key for key in settings.missing_secrets() if key in ESPN_ENV_KEYS]


def _cmd_sync(_args: argparse.Namespace) -> int:
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


def _cmd_job(args: argparse.Namespace) -> int:
    """Run one job on demand.

    The board is researched the day before the draft and takes minutes, so it
    needs a way to be started by hand — and to be startable again when it fails
    at six in the morning. The scheduler is Task 9; this is the command that
    makes the board buildable today.
    """
    from hal_mary.jobs.registry import JOBS, job_names

    settings = load_cli_settings()
    job = JOBS.get(args.name)
    if job is None:
        print(
            f"unknown job {args.name!r}. This build knows: {job_names()}.",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    conn = open_db(settings)
    outcome = job(conn, settings, build_runner(settings, conn))

    if not outcome.get("ok", True):
        print(f"{args.name} failed: {outcome.get('error')}", file=sys.stderr)
        return EXIT_JOB_FAILED
    print(f"{args.name}: {outcome.get('summary') or 'done'}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hal-mary", description=DESCRIPTION, epilog=EPILOG)
    subcommands = parser.add_subparsers(
        dest="command", metavar="{sync,espn-check,serve,job}"
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
    job.add_argument("name", help="which job to run, e.g. board_build")
    job.set_defaults(handler=_cmd_job)

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
