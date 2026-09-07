"""Tests for the committed non-code files config points at.

These drift silently: config.toml names a prompt file, .env.example names the
secrets, and nothing fails until a job runs at 6am. Pin them here instead.
"""

from pathlib import Path

from hal_mary.config import ENV_KEYS, load_settings

REPO = Path(__file__).resolve().parents[2]


def test_env_example_documents_every_env_key():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    missing = [key for key in ENV_KEYS if f"{key}=" not in text]
    assert missing == []


def test_env_example_carries_no_real_values():
    """Every assignment is empty or an obvious placeholder — secrets never enter git."""
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        assert key in ENV_KEYS, f"unexpected key in .env.example: {key}"
        assert value == "" or value.startswith("<"), f"{key} looks like a real value"


def test_system_prompt_file_exists_and_is_substantial():
    settings = load_settings(env={})
    prompt = REPO / settings.claude.system_prompt_file
    assert prompt.is_file()
    assert len(prompt.read_text(encoding="utf-8")) > 500


def test_configured_directories_exist():
    settings = load_settings(env={})
    assert (REPO / settings.paths.prompts_dir).is_dir()
    assert (REPO / settings.paths.memory_dir).is_dir()


def test_standing_memory_files_exist():
    settings = load_settings(env={})
    memory = REPO / settings.paths.memory_dir
    assert (memory / "caroline.md").is_file()
    assert (memory / "league.md").is_file()


def test_memory_files_are_marked_human_editable():
    settings = load_settings(env={})
    text = (REPO / settings.paths.memory_dir / "caroline.md").read_text(encoding="utf-8")
    assert "human-editable" in text.lower()


#: Task 5's rewrite of memory/league.md must preserve everything below this line
#: verbatim. Pinned here because it is a contract between two tasks, not prose.
LEAGUE_PRESERVE_SENTINEL = "<!-- hal-mary:preserve-below -->"


def test_league_memory_has_a_machine_readable_preserve_sentinel():
    settings = load_settings(env={})
    text = (REPO / settings.paths.memory_dir / "league.md").read_text(encoding="utf-8")
    assert text.count(LEAGUE_PRESERVE_SENTINEL) == 1
    before, _, after = text.partition(LEAGUE_PRESERVE_SENTINEL)
    assert "---" in before, "the sentinel is preceded by a horizontal rule"
    assert after.strip(), "the sentinel is not the last line; hand-written notes go below it"


def test_league_memory_explains_what_the_sentinel_means():
    settings = load_settings(env={})
    text = (REPO / settings.paths.memory_dir / "league.md").read_text(encoding="utf-8")
    assert "hal-mary sync" in text
    assert "preserved" in text.lower()
