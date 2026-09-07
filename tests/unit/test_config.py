"""Tests for the configuration layer.

Config is the only place model names, timeouts, cadences and budgets live, so
these tests are the contract every other module reads through.
"""

import textwrap

import pytest
from pydantic import ValidationError

from hal_mary.config import ENV_KEYS, ConfigError, Settings, load_settings

MINIMAL_TOML = """
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

[jobs.inherits_model]
tools = []
timeout_s = 45
max_budget_usd = 1.0
enabled = true

[jobs.explicit_model]
model = "haiku-test"
tools = ["WebSearch"]
timeout_s = 900
max_budget_usd = 5.0
enabled = true
cron = "0 6 * * *"
"""

FULL_ENV = {
    "ESPN_S2": "cookie-s2",
    "SWID": "{swid}",
    "LEAGUE_ID": "12345",
    "TEAM_ID": "7",
    "SEASON": "2026",
    "WEB_PASSWORD": "hunter2",
    "DB_PATH": "/var/lib/hal.db",
}


@pytest.fixture
def minimal_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(MINIMAL_TOML))
    return path


def test_loads_the_repo_config_toml_by_default():
    settings = load_settings(env={})
    assert settings.claude.binary == "claude"
    assert settings.web.port == 8080
    assert settings.draft.poll_seconds == 5
    assert settings.paths.prompts_dir == "prompts"
    assert "draft_advice" in settings.jobs


def test_reads_sections_from_an_explicit_toml_path(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    assert settings.claude.permission_mode == "dontAsk"
    assert settings.claude.scratch_dir == ".scratch"
    assert settings.web.session_cookie == "hal_mary_session"
    assert settings.draft.advise_within_picks == 2


def test_env_overlay_populates_secrets(minimal_config):
    settings = load_settings(config_path=minimal_config, env=FULL_ENV)
    assert settings.espn_s2 == "cookie-s2"
    assert settings.swid == "{swid}"
    assert settings.web_password == "hunter2"
    assert settings.db_path == "/var/lib/hal.db"


def test_numeric_env_keys_coerce_to_int(minimal_config):
    settings = load_settings(config_path=minimal_config, env=FULL_ENV)
    assert settings.league_id == 12345
    assert settings.team_id == 7
    assert settings.season == 2026


@pytest.mark.parametrize("key", ["LEAGUE_ID", "TEAM_ID", "SEASON"])
def test_non_numeric_int_env_raises_naming_the_key(minimal_config, key):
    env = dict(FULL_ENV, **{key: "not-a-number"})
    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=minimal_config, env=env)
    assert key in str(excinfo.value)
    assert "not-a-number" in str(excinfo.value)


def test_missing_secrets_do_not_raise_and_are_reported(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    assert settings.espn_s2 is None
    assert settings.league_id is None
    assert set(settings.missing_secrets()) == {
        "ESPN_S2",
        "SWID",
        "LEAGUE_ID",
        "TEAM_ID",
        "SEASON",
        "WEB_PASSWORD",
    }


def test_missing_secrets_is_empty_when_everything_is_set(minimal_config):
    settings = load_settings(config_path=minimal_config, env=FULL_ENV)
    assert settings.missing_secrets() == []


def test_missing_secrets_lists_only_the_absent_keys(minimal_config):
    env = {"ESPN_S2": "x", "SWID": "y", "LEAGUE_ID": "1"}
    settings = load_settings(config_path=minimal_config, env=env)
    assert set(settings.missing_secrets()) == {"TEAM_ID", "SEASON", "WEB_PASSWORD"}


def test_db_path_defaults_when_unset(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    assert settings.db_path == "./hal.db"
    assert "DB_PATH" not in settings.missing_secrets()


def test_job_lookup_returns_the_job_config(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    job = settings.job("explicit_model")
    assert job.model == "haiku-test"
    assert job.tools == ["WebSearch"]
    assert job.timeout_s == 900
    assert job.max_budget_usd == 5.0
    assert job.enabled is True
    assert job.cron == "0 6 * * *"


def test_job_without_cron_is_none(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    assert settings.job("inherits_model").cron is None


def test_unknown_job_raises_keyerror_listing_valid_names(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    with pytest.raises(KeyError) as excinfo:
        settings.job("no_such_job")
    message = str(excinfo.value)
    assert "no_such_job" in message
    assert "inherits_model" in message
    assert "explicit_model" in message


def test_job_omitting_model_inherits_the_default_model(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    assert settings.job("inherits_model").model == settings.claude.default_model
    assert settings.job("inherits_model").model == "sonnet-test"


def test_settings_are_frozen(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    with pytest.raises(ValidationError):
        settings.db_path = "/somewhere/else"
    with pytest.raises(ValidationError):
        settings.claude.default_model = "something"
    with pytest.raises(ValidationError):
        settings.job("explicit_model").timeout_s = 1


def test_settings_is_exported_as_a_type(minimal_config):
    assert isinstance(load_settings(config_path=minimal_config, env={}), Settings)


# --- path overrides for deployments whose cwd is not the source tree ----------


def test_hal_mary_config_env_var_selects_the_config_file(minimal_config):
    settings = load_settings(env={"HAL_MARY_CONFIG": str(minimal_config)})
    assert settings.claude.default_model == "sonnet-test"


def test_explicit_config_path_wins_over_the_env_var(minimal_config, tmp_path):
    other = tmp_path / "other.toml"
    other.write_text(textwrap.dedent(MINIMAL_TOML).replace("sonnet-test", "other-model"))
    settings = load_settings(config_path=minimal_config, env={"HAL_MARY_CONFIG": str(other)})
    assert settings.claude.default_model == "sonnet-test"


def test_hal_mary_config_pointing_at_nothing_raises_naming_the_variable(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env={"HAL_MARY_CONFIG": str(tmp_path / "absent.toml")})
    assert "HAL_MARY_CONFIG" in str(excinfo.value)


def test_hal_mary_env_var_selects_the_dotenv_file(minimal_config, tmp_path, monkeypatch):
    dotenv = tmp_path / "prod.env"
    dotenv.write_text("ESPN_S2=from-the-file\nLEAGUE_ID=99\n")
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HAL_MARY_CONFIG", str(minimal_config))
    monkeypatch.setenv("HAL_MARY_ENV", str(dotenv))
    settings = load_settings()
    assert settings.espn_s2 == "from-the-file"
    assert settings.league_id == 99


def test_real_environment_wins_over_the_dotenv_file(minimal_config, tmp_path, monkeypatch):
    dotenv = tmp_path / "prod.env"
    dotenv.write_text("ESPN_S2=from-the-file\n")
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HAL_MARY_CONFIG", str(minimal_config))
    monkeypatch.setenv("HAL_MARY_ENV", str(dotenv))
    monkeypatch.setenv("ESPN_S2", "from-the-environment")
    settings = load_settings()
    assert settings.espn_s2 == "from-the-environment"


def test_hal_mary_env_pointing_at_nothing_raises_naming_the_variable(
    minimal_config, tmp_path, monkeypatch
):
    """A typo'd path must be loud. Silently loading no secrets is the failure
    this override exists to prevent."""
    monkeypatch.setenv("HAL_MARY_CONFIG", str(minimal_config))
    monkeypatch.setenv("HAL_MARY_ENV", str(tmp_path / "absent.env"))
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "HAL_MARY_ENV" in str(excinfo.value)


def test_an_explicit_env_mapping_ignores_the_dotenv_overrides(minimal_config, monkeypatch):
    """A mapping is used verbatim: no file is read, so tests stay hermetic."""
    monkeypatch.setenv("HAL_MARY_ENV", "/does/not/exist.env")
    settings = load_settings(config_path=minimal_config, env={"ESPN_S2": "explicit"})
    assert settings.espn_s2 == "explicit"
    assert settings.swid is None
