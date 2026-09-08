"""The verdict the draft watcher prints is the whole point of the script.

Whether ESPN publishes picks to the read API *while* a draft runs is still
unverified. The script exists to answer that in thirty seconds on the night, so
its reasoning is tested here rather than being a print statement nobody read
until it mattered.

The tie-break is rosters. `picks` alone cannot distinguish "no draft yet" from
"a draft nobody can see", and those two want opposite things done about them —
one is patience, the other is "start typing picks in by hand".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "watch-draft.py"


@pytest.fixture(scope="module")
def watcher():
    """The script loaded as a module. It is an operator tool, not a package."""
    spec = importlib.util.spec_from_file_location("watch_draft", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_pick_on_the_board_settles_it(watcher):
    """One real pick proves the board is live; nothing else matters."""
    verdict = watcher.read_verdict(1, 0, False)

    assert verdict is not None
    assert "publishes picks live" in verdict


def test_players_on_rosters_with_an_empty_board_is_the_bad_answer(watcher):
    """The case the manual pick path exists for.

    A drafted player lands on a roster whether or not the board is published,
    so rosters filling while `picks` stays empty is ESPN withholding them.
    """
    verdict = watcher.read_verdict(0, 7, True)

    assert verdict is not None
    assert "NOT" in verdict
    assert "by hand" in verdict


def test_nothing_anywhere_is_not_an_answer_yet(watcher):
    """An open lobby with nothing picked is patience, not a finding."""
    verdict = watcher.read_verdict(0, 0, True)

    assert verdict is not None
    assert "waiting" in verdict
    # It must not claim ESPN is withholding: nothing has been drafted at all.
    assert "NOT publishing" not in verdict


def test_a_quiet_league_says_nothing_at_all(watcher):
    """No lobby, no picks, no rosters: there is nothing to report."""
    assert watcher.read_verdict(0, 0, False) is None


def test_in_progress_never_decides_the_verdict(watcher):
    """`inProgress` describes the lobby, not the picks.

    A full 12-team room with a drawn order reported `inProgress: True` for the
    whole of one mock draft that produced no picks and no rostered players. If
    this flag could move the verdict, that night would have produced a
    confident wrong answer.
    """
    for flag in (True, False, None):
        assert "publishes picks live" in watcher.read_verdict(3, 0, flag)
        assert "NOT" in watcher.read_verdict(0, 5, flag)
