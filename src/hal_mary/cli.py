"""Command line entry point.

A stub for now: later tasks add ``serve``, ``sync``, ``job`` and ``draft-spike``.
It deliberately does no configuration or database work at import or on ``--help``,
so a fresh checkout with no ``.env`` can still be interrogated.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

__all__ = ["build_parser", "main"]

DESCRIPTION = "hal-mary — a Claude-powered fantasy football advisor."

EPILOG = "Subcommands (serve, sync, job, draft-spike) are added by later tasks."


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="hal-mary", description=DESCRIPTION, epilog=EPILOG)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
