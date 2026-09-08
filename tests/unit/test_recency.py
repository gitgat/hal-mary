"""Research must be anchored to today, not to whenever the model was trained.

The season is live and the model's cutoff is not. Before this existed, no
research prompt was told what day it was: `board_build.md` said "prefer recent
sources" and `news_sweep.md` said "a stale claim is worse than silence", and
neither gave the model a way to know what recent *meant*. A model with no anchor
treats its own training cutoff as the present, which is exactly the failure this
repo's sixth hard rule exists to prevent.

Python owns the arithmetic here — the date, the cutoffs, the wording — because
it is the half that can be tested.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import FIXTURE_ENV
from hal_mary.config import load_settings
from hal_mary.recency import recency_block

REPO = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 8, 17, 30, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(env={**FIXTURE_ENV, "DB_PATH": str(tmp_path / "hal.db")})


def test_the_block_says_what_day_it_is_in_words(settings):
    """A date the model cannot misread as a training artefact."""
    block = recency_block(settings, now=MOMENT)

    assert "2026" in block
    assert "September" in block
    assert "8" in block


def test_the_block_carries_the_configured_windows(settings):
    """The numbers come from config.toml, never from the prose."""
    block = recency_block(settings, now=MOMENT)

    assert str(settings.research.recency_current_days) in block
    assert str(settings.research.recency_stale_days) in block


def test_changing_the_window_changes_the_block(settings, tmp_path):
    """Pins the value to config rather than to a sentence someone typed."""
    widened = settings.model_copy(
        update={"research": settings.research.model_copy(update={"recency_current_days": 9})}
    )

    assert "9" in recency_block(widened, now=MOMENT)


def test_the_block_refuses_training_knowledge_as_a_source(settings):
    """The specific failure: answering from the cutoff and sounding certain."""
    block = recency_block(settings, now=MOMENT).lower()

    assert "training" in block
    assert "not a source" in block or "is not a source" in block


def test_the_block_asks_for_the_date_of_each_claim(settings):
    """Undated research cannot be audited for staleness afterwards."""
    block = recency_block(settings, now=MOMENT).lower()

    assert "date" in block


def test_the_block_is_one_line_safe_for_a_prompt(settings):
    """No stray placeholder braces: `render` would raise on the way out."""
    block = recency_block(settings, now=MOMENT)

    assert "{{" not in block and "}}" not in block


# --- the part that fails closed ----------------------------------------------


#: Every module that runs a Claude call with web tools, and the prompt it uses.
#: A new one lands here or the test below tells its author why.
WEB_RESEARCH_PROMPTS = {
    "board_build": "board_build.md",
    "news_sweep": "news_sweep.md",
    "waiver_scan": "waiver_scan.md",
    "lineup_check": "lineup_check.md",
    "weekly_recap": "weekly_recap.md",
    "chat": "chat.md",
}


def test_every_web_enabled_job_is_in_the_recency_list(settings):
    """The list above is the allowlist; config decides who belongs on it.

    A job that gains `WebSearch` and is not listed fails here rather than
    quietly researching with no idea what today is.
    """
    web_enabled = {
        name
        for name, job in settings.jobs.items()
        if any(tool.startswith("Web") for tool in (job.tools or []))
    }

    assert web_enabled <= set(WEB_RESEARCH_PROMPTS), (
        f"these jobs search the web but carry no recency block: "
        f"{sorted(web_enabled - set(WEB_RESEARCH_PROMPTS))}"
    )


@pytest.mark.parametrize("prompt_file", sorted(set(WEB_RESEARCH_PROMPTS.values())))
def test_every_research_prompt_asks_for_the_recency_block(prompt_file):
    """The placeholder, not a copy of the words.

    `prompts.render` raises on an unfilled placeholder, so a job that forgets to
    supply it fails loudly instead of sending undated research to a model.
    """
    text = (REPO / "prompts" / prompt_file).read_text(encoding="utf-8")

    assert re.search(r"\{\{\s*recency\s*\}\}", text), (
        f"prompts/{prompt_file} does not include {{{{recency}}}}"
    )
