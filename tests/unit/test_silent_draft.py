"""When the draft is running but ESPN publishes nothing, say so.

We do not know whether ESPN populates `draftDetail.picks` while a draft is in
progress — a mock draft that genuinely began showed zero picks and zero rostered
players for the whole time it was watched, and the room was then deleted, so the
question is still open. hal-mary therefore has to be right either way.

The silent case is the dangerous one, and it is dangerous precisely because it
looks fine: the loop is on draft-night cadence, the page says "hal-mary is
watching ESPN every 5 seconds", and Caroline reads that as *working*. Meanwhile
the real draft is moving without her, the board never marks anyone gone, and the
advice is about players who left the board twenty minutes ago.

So the loop reports how long it has been live with nothing to show for it, and
past `draft.silent_after_seconds` the page stops reassuring her and tells her to
enter the picks by hand — the path that needs no ESPN at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FIXTURE_ENV
from hal_mary.config import load_settings
from hal_mary.draft.loop import PHASE_IDLE, PHASE_LIVE


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(env={**FIXTURE_ENV, "DB_PATH": str(tmp_path / "hal.db")})


def _watching(settings, **over):
    from hal_mary.web.draft_page import _watching as w

    reported = {
        "phase": PHASE_LIVE,
        "poll_seconds": settings.draft.poll_seconds,
        "cadence": "every 5 seconds",
        "live_seconds": 0.0,
        "picks_seen": 0,
    }
    reported.update(over)
    return w(settings, None, 1, None, reported)


def test_a_live_loop_that_has_just_started_is_not_called_silent(settings):
    """A draft that opened ten seconds ago has simply not had a pick yet."""
    assert _watching(settings, live_seconds=10.0)["silent"] is False


def test_a_live_loop_with_no_picks_for_too_long_is_silent(settings):
    """Past the threshold, "watching" is no longer the useful thing to say."""
    over = settings.draft.silent_after_seconds + 1

    assert _watching(settings, live_seconds=over)["silent"] is True


def test_a_loop_that_has_seen_a_pick_is_never_silent(settings):
    """One real pick proves ESPN is publishing; nothing to warn about."""
    over = settings.draft.silent_after_seconds + 600

    assert _watching(settings, live_seconds=over, picks_seen=1)["silent"] is False


def test_an_idle_loop_is_never_silent(settings):
    """No draft is running, so there is nothing to be silent about."""
    over = settings.draft.silent_after_seconds + 600

    assert _watching(settings, phase=PHASE_IDLE, live_seconds=over)["silent"] is False


def test_the_threshold_comes_from_config(settings):
    """Not a number typed into a template."""
    tight = settings.model_copy(
        update={"draft": settings.draft.model_copy(update={"silent_after_seconds": 5})}
    )

    assert _watching(tight, live_seconds=6.0)["silent"] is True
    assert _watching(tight, live_seconds=4.0)["silent"] is False
