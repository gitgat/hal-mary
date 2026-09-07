"""Job name to callable, for ``hal-mary job <name>``.

Deliberately a plain dict. Task 9 brings the scheduler and with it the real
registry — enabled flags, cron expressions, overlap rules — and inventing that
here would mean writing it twice. What the draft needs today is one command that
builds the board from a terminal on the box, because a board that can only be
built by a scheduler that does not exist yet is a board that does not get built.

Every entry takes ``(conn, settings, runner)`` and returns a summary dict with
``ok``. That is the shape ``jobs.board_build.build_board`` already has, and
matching it is what keeps this a lookup table rather than an adapter layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hal_mary.jobs.board_build import build_board

__all__ = ["JOBS", "job_names"]

JOBS: dict[str, Callable[..., dict[str, Any]]] = {
    "board_build": build_board,
}


def job_names() -> str:
    """The jobs this build knows, for an error message that helps."""
    return ", ".join(sorted(JOBS))
