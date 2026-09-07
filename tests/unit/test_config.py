"""Tests for the configuration layer.

Config is the only place model names, timeouts, cadences and budgets live, so
these tests are the contract every other module reads through.
"""

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from hal_mary.config import (
    ENV_KEYS,
    ClaudeConfig,
    ConfigError,
    DraftConfig,
    PathsConfig,
    Settings,
    WebConfig,
    load_settings,
)

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

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    assert settings.paths.prompts_dir == REPO_ROOT / "prompts"
    assert "draft_advice" in settings.jobs


def test_reads_sections_from_an_explicit_toml_path(minimal_config):
    settings = load_settings(config_path=minimal_config, env={})
    assert settings.claude.permission_mode == "dontAsk"
    assert settings.claude.scratch_dir == minimal_config.parent / ".scratch"
    assert settings.web.session_cookie == "hal_mary_session"
    assert settings.draft.advise_within_picks == 2


def test_env_overlay_populates_secrets(minimal_config):
    settings = load_settings(config_path=minimal_config, env=FULL_ENV)
    assert settings.espn_s2 == "cookie-s2"
    assert settings.swid == "{swid}"
    assert settings.web_password == "hunter2"
    assert settings.db_path == Path("/var/lib/hal.db")


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
    # Anchored like every other path: a cwd-relative default under systemd
    # creates a second, empty database beside the unit's working directory.
    assert settings.db_path == minimal_config.parent / "hal.db"
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


def test_espn_timeouts_are_read_from_the_toml(tmp_path):
    """Transport limits are config, not constants: CLAUDE.md rule 5 names timeouts."""
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(MINIMAL_TOML) + "\n[espn]\nconnect_timeout_s = 3.5\nread_timeout_s = 7.0\n"
    )

    settings = load_settings(config_path=path, env={})

    assert settings.espn.connect_timeout_s == 3.5
    assert settings.espn.read_timeout_s == 7.0


def test_espn_section_is_optional_and_defaults_are_sane(minimal_config):
    """An older config.toml still loads; the defaults are bounded by the pick clock."""
    settings = load_settings(config_path=minimal_config, env={})

    assert settings.espn.connect_timeout_s > 0
    assert settings.espn.read_timeout_s > settings.draft.poll_seconds


def test_the_shipped_config_declares_the_espn_timeouts():
    settings = load_settings(env={})

    assert settings.espn.connect_timeout_s == 10.0
    assert settings.espn.read_timeout_s == 15.0


def test_web_section_carries_the_session_and_stream_tunables(minimal_config):
    """Session lifetime, heartbeat and auth-check cadence are config, not code.

    They have defaults so a config.toml written before the web app existed still
    loads — the systemd unit ships one, and a deploy that could not read its own
    config would be a worse failure than a stale default.
    """
    defaults = load_settings(config_path=minimal_config, env={}).web
    assert defaults.session_max_age_days > 0
    assert defaults.sse_heartbeat_s > 0
    assert defaults.auth_check_seconds > 0
    # A config that never heard of a login limit still gets one: it is the only
    # thing between a device on the LAN and guessing the shared password.
    assert defaults.login_max_attempts > 0
    assert defaults.login_lockout_seconds > 0

    repo = load_settings(env={}).web
    assert repo.sse_heartbeat_s == 15.0
    assert repo.auth_check_seconds == 3600
    assert repo.login_max_attempts == 5
    assert repo.login_lockout_seconds == 60.0


# --- path anchoring ----------------------------------------------------------
#
# Every relative path in config.toml is resolved against *the directory holding
# config.toml*, not the process working directory. Under the systemd unit the
# working directory is not the source tree, and a memory_dir that resolved
# against it would find nothing, return "" and quietly strip the standing
# context out of every prompt. No crash, no log line, just worse advice.


def test_relative_paths_anchor_to_the_config_files_directory(minimal_config, tmp_path, monkeypatch):
    """The proof: resolve with the working directory somewhere else entirely."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    settings = load_settings(config_path=minimal_config, env={})

    assert settings.paths.memory_dir == tmp_path / "memory"
    assert settings.paths.prompts_dir == tmp_path / "prompts"
    assert settings.claude.scratch_dir == tmp_path / ".scratch"
    assert settings.claude.system_prompt_file == tmp_path / "prompts" / "system.md"


def test_resolved_paths_are_path_objects_not_strings(minimal_config):
    """Callers get something already resolved, so none of them can re-resolve it
    against the wrong anchor."""
    settings = load_settings(config_path=minimal_config, env={})

    for value in (
        settings.paths.memory_dir,
        settings.paths.prompts_dir,
        settings.claude.scratch_dir,
        settings.claude.system_prompt_file,
    ):
        assert isinstance(value, Path), f"{value!r} is not a Path"
        assert value.is_absolute(), f"{value} is not absolute"


def test_absolute_paths_in_config_are_left_exactly_as_given(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(MINIMAL_TOML)
        .replace('memory_dir = "memory"', 'memory_dir = "/srv/hal/memory"')
        .replace('scratch_dir = ".scratch"', 'scratch_dir = "/var/tmp/hal-scratch"')
    )

    settings = load_settings(config_path=path, env={})

    assert settings.paths.memory_dir == Path("/srv/hal/memory")
    assert settings.claude.scratch_dir == Path("/var/tmp/hal-scratch")


def test_hal_mary_config_anchors_paths_at_that_files_directory(tmp_path, monkeypatch):
    """The override moves the anchor with it; that is the whole point of it."""
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    config = deployed / "config.toml"
    config.write_text(textwrap.dedent(MINIMAL_TOML))
    monkeypatch.chdir(tmp_path)

    settings = load_settings(env={"HAL_MARY_CONFIG": str(config)})

    assert settings.paths.memory_dir == deployed / "memory"
    assert settings.config_path == config


def test_a_relative_db_path_anchors_too(minimal_config, tmp_path, monkeypatch):
    """Same defect, different file: a cwd-relative DB_PATH under systemd creates
    a second, empty database instead of opening the real one."""
    monkeypatch.chdir(tmp_path / "..")

    settings = load_settings(config_path=minimal_config, env={"DB_PATH": "./hal.db"})

    assert settings.db_path == tmp_path / "hal.db"



def test_resolved_paths_reports_each_path_and_whether_it_exists(minimal_config, tmp_path):
    """What the status page renders, so a wrong anchor is diagnosable without
    an SSH session."""
    (tmp_path / "memory").mkdir()

    settings = load_settings(config_path=minimal_config, env={})
    report = {label: (path, exists) for label, path, exists in settings.resolved_paths()}

    assert report["Memory"] == (tmp_path / "memory", True)
    assert report["Prompts"] == (tmp_path / "prompts", False)
    assert report["Config file"] == (minimal_config, True)


def test_a_relative_config_path_still_yields_absolute_paths(tmp_path, monkeypatch):
    """The validator's promise is that *no* route to a Settings produces a
    cwd-relative path. ``config_path`` is a public field, so a future loader or
    test helper setting a relative one must not reintroduce the bug with the
    guard apparently still standing.
    """
    (tmp_path / "config.toml").write_text(textwrap.dedent(MINIMAL_TOML))
    monkeypatch.chdir(tmp_path)

    loaded = load_settings(config_path=tmp_path / "config.toml", env={})
    # model_validate is the route a future loader would take; the relative
    # config_path is what it might plausibly hand over.
    settings = Settings.model_validate({**loaded.model_dump(), "config_path": "config.toml"})

    assert settings.config_path.is_absolute()
    assert settings.paths.memory_dir == tmp_path / "memory"
    assert settings.db_path == tmp_path / "hal.db"
    assert settings.claude.scratch_dir.is_absolute()


def test_settings_constructed_directly_with_a_relative_config_path_anchors_absolutely(
    tmp_path, monkeypatch
):
    """The same thing by the shortest route: a plain constructor call."""
    monkeypatch.chdir(tmp_path)

    settings = Settings(
        config_path=Path("deploy/config.toml"),
        claude=ClaudeConfig(
            default_model="m",
            permission_mode="dontAsk",
            scratch_dir=".scratch",
            system_prompt_file="prompts/system.md",
        ),
        paths=PathsConfig(prompts_dir="prompts", memory_dir="memory"),
        draft=DraftConfig(poll_seconds=5, advise_within_picks=2),
        web=WebConfig(host="127.0.0.1", port=8080, session_cookie="c"),
        jobs={},
    )

    assert settings.paths.memory_dir == tmp_path / "deploy" / "memory"
    assert settings.claude.system_prompt_file == tmp_path / "deploy" / "prompts" / "system.md"
    assert settings.db_path == tmp_path / "deploy" / "hal.db"
