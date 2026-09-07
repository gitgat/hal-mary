"""Every consumer of a configured path, driven from a working directory that is
not the checkout.

This is the regression test for the defect the rest of this module's siblings
only cover a piece of each. Two implementers hit it independently: relative
paths in ``config.toml`` were resolved against the *process* working directory,
which is the checkout for a developer and something else entirely for the
systemd unit. The failure was not a crash — ``standing_memory()`` found no
directory, returned ``""``, and every prompt went out without the standing
context that says who Caroline is and what her league's rules are. The service
looked healthy and the advice quietly got worse.

So each test here does the same thing: build a complete little deployment in
``tmp_path``, chdir somewhere with none of it, and check the consumer still
reads the right file.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hal_mary import memory, prompts
from hal_mary.claude_runner import ClaudeRunner
from hal_mary.config import load_settings
from hal_mary.espn.sync import league_memory_path

CONFIG = """
[claude]
binary = "claude"
default_model = "sonnet-test"
permission_mode = "dontAsk"
scratch_dir = ".scratch"
system_prompt_file = "prompts/system.md"

[paths]
prompts_dir = "prompts"
memory_dir = "memory"

[draft]
poll_seconds = 5
advise_within_picks = 2

[web]
host = "0.0.0.0"
port = 8080
session_cookie = "hal_mary_session"

[jobs.chat]
tools = []
timeout_s = 45
max_budget_usd = 1.0
"""


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout in ``tmp_path``, with the working directory somewhere else."""
    root = tmp_path / "checkout"
    (root / "prompts").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / "config.toml").write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    (root / "prompts" / "system.md").write_text("You advise on fantasy football.\n")
    (root / "prompts" / "chat.md").write_text("Answer {{question}}.\n")
    (root / "memory" / "caroline.md").write_text("She is new to fantasy football.\n")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return root


def settings_for(root: Path):
    return load_settings(config_path=root / "config.toml", env={})


def test_standing_memory_is_found_from_another_working_directory(deployment: Path):
    """The defect itself: this returned "" and nobody noticed."""
    text = memory.standing_memory(settings_for(deployment))

    assert "She is new to fantasy football." in text


def test_a_prompt_file_is_found_from_another_working_directory(deployment: Path):
    text = prompts.render_prompt(
        settings_for(deployment), "chat.md", {"question": "who do I start?"}
    )

    assert text.strip() == "Answer who do I start?."


def test_the_system_prompt_is_found_from_another_working_directory(deployment: Path, tmp_path):
    from hal_mary import db

    conn = db.connect(tmp_path / "hal.db")
    db.migrate(conn)
    runner = ClaudeRunner(settings_for(deployment), conn)

    assert runner.resolve_system_prompt(None) == "You advise on fantasy football."
    conn.close()


def test_the_scratch_dir_lands_beside_the_config_not_the_working_directory(
    deployment: Path, tmp_path: Path
):
    """It is the subprocess cwd. Created under whatever directory a service
    manager happened to start in, it is litter nobody goes looking for."""
    from hal_mary import db

    conn = db.connect(tmp_path / "hal.db")
    db.migrate(conn)

    scratch = ClaudeRunner(settings_for(deployment), conn).scratch_dir()

    assert scratch == deployment / ".scratch"
    assert not (Path.cwd() / ".scratch").exists()
    conn.close()


def test_the_generated_league_memory_lands_in_the_configured_memory_dir(deployment: Path):
    """``hal-mary sync`` writes this file; standing memory reads it. If those two
    disagree about where it lives, the sync succeeds and the context is empty."""
    assert league_memory_path(settings_for(deployment)).parent == deployment / "memory"


def test_the_sync_writes_where_standing_memory_reads(deployment: Path):
    settings = settings_for(deployment)
    league_memory_path(settings).write_text("Six-team full PPR.\n", encoding="utf-8")

    assert "Six-team full PPR." in memory.standing_memory(settings)
