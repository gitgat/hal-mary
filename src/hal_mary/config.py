"""Configuration for hal-mary.

Everything tunable — models, tool allowlists, timeouts, budgets, cadences,
paths and ports — lives in ``config.toml`` at the repo root. Secrets live in the
process environment (loaded from ``.env`` when present). Nothing else in ``src/``
may hardcode any of those values.

Missing secrets are deliberately *not* an error at load time: ``hal-mary --help``
and the test suite have to work on a box with no ``.env``, and the status page
wants to report exactly which keys are unset rather than crash.

Two environment variables override where those files are found, for deployments
whose working directory is not the source tree (the systemd unit, for one):

* ``HAL_MARY_CONFIG`` — path to ``config.toml``
* ``HAL_MARY_ENV`` — path to the ``.env`` file

Both are loud when they point at nothing: a typo'd path that silently loaded no
secrets is exactly the failure they exist to prevent.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

__all__ = [
    "ClaudeConfig",
    "ConfigError",
    "DraftConfig",
    "EspnConfig",
    "JobConfig",
    "PathsConfig",
    "Settings",
    "WebConfig",
    "load_settings",
]

#: Environment keys read into :class:`Settings`.
ENV_KEYS = ("ESPN_S2", "SWID", "LEAGUE_ID", "TEAM_ID", "SEASON", "WEB_PASSWORD", "DB_PATH")

#: Keys that must be present before hal-mary can talk to ESPN or serve the web app.
#: ``DB_PATH`` is absent on purpose: it has a default.
REQUIRED_ENV_KEYS = ("ESPN_S2", "SWID", "LEAGUE_ID", "TEAM_ID", "SEASON", "WEB_PASSWORD")

#: Env keys whose values are integers.
INT_ENV_KEYS = ("LEAGUE_ID", "TEAM_ID", "SEASON")

#: Deployment overrides. These are paths, not secrets, and are read from the
#: process environment before anything else. The systemd unit sets them because
#: its WorkingDirectory is not the source tree, so neither the repo-root nor the
#: cwd guess below finds the right files.
CONFIG_PATH_ENV = "HAL_MARY_CONFIG"
DOTENV_PATH_ENV = "HAL_MARY_ENV"

DEFAULT_DB_PATH = "./hal.db"

# src/hal_mary/config.py -> src/hal_mary -> src -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when config.toml or the environment cannot be turned into Settings."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClaudeConfig(_Frozen):
    """How to invoke the local ``claude`` binary."""

    binary: str = "claude"
    default_model: str
    permission_mode: str
    scratch_dir: str
    system_prompt_file: str


class PathsConfig(_Frozen):
    prompts_dir: str
    memory_dir: str


class DraftConfig(_Frozen):
    poll_seconds: int
    advise_within_picks: int


class EspnConfig(_Frozen):
    """Transport limits for the raw ESPN reads.

    These belong here rather than in the client because they are bounded by the
    thing configured immediately above them: the pick clock is 60 to 90 seconds
    and ``draft.poll_seconds`` is 5, so a read that outlives its poll silently
    stops the draft loop. Defaults are supplied so an older ``config.toml``
    without an ``[espn]`` section still loads.
    """

    connect_timeout_s: float = 10.0
    read_timeout_s: float = 15.0


class WebConfig(_Frozen):
    host: str
    port: int
    session_cookie: str


class JobConfig(_Frozen):
    """One entry from ``[jobs.*]``.

    ``model`` is filled in from ``claude.default_model`` at load time when the
    job does not name one, so consumers never have to know about the fallback.
    """

    name: str
    model: str
    tools: list[str] = []
    timeout_s: int
    max_budget_usd: float
    enabled: bool = True
    cron: str | None = None


class Settings(_Frozen):
    """The whole resolved configuration: config.toml plus the environment."""

    claude: ClaudeConfig
    paths: PathsConfig
    draft: DraftConfig
    espn: EspnConfig = EspnConfig()
    web: WebConfig
    jobs: dict[str, JobConfig]

    espn_s2: str | None = None
    swid: str | None = None
    league_id: int | None = None
    team_id: int | None = None
    season: int | None = None
    web_password: str | None = None
    db_path: str = DEFAULT_DB_PATH

    def job(self, name: str) -> JobConfig:
        """Return the config for job ``name``.

        Raises ``KeyError`` naming the configured jobs, because a typo in a job
        name otherwise surfaces far away from its cause.
        """
        try:
            return self.jobs[name]
        except KeyError:
            known = ", ".join(sorted(self.jobs)) or "(none configured)"
            raise KeyError(f"unknown job {name!r}; configured jobs: {known}") from None

    def missing_secrets(self) -> list[str]:
        """Environment keys that are required but unset, in declaration order."""
        values = {
            "ESPN_S2": self.espn_s2,
            "SWID": self.swid,
            "LEAGUE_ID": self.league_id,
            "TEAM_ID": self.team_id,
            "SEASON": self.season,
            "WEB_PASSWORD": self.web_password,
        }
        return [key for key in REQUIRED_ENV_KEYS if values[key] in (None, "")]


def _override_path(source: Mapping[str, str], key: str) -> Path | None:
    """Read a path override, insisting that it exists."""
    raw = source.get(key)
    if raw in (None, ""):
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ConfigError(f"{key} points at {path}, which is not a file")
    return path


def _resolve_config_path(source: Mapping[str, str]) -> Path:
    override = _override_path(source, CONFIG_PATH_ENV)
    if override is not None:
        return override
    for candidate in (_REPO_ROOT / "config.toml", Path.cwd() / "config.toml"):
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"config.toml not found (looked in {_REPO_ROOT} and {Path.cwd()}); "
        f"set {CONFIG_PATH_ENV} to point at it"
    )


def _resolve_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Collect the env keys we care about.

    When ``env`` is None we read ``.env`` from the repo root and let real
    environment variables win over it. When a mapping is passed (tests, and any
    caller wanting hermetic behaviour) it is used verbatim: no ``.env`` is read,
    so a stray file on the box cannot change a test's answer.
    """
    if env is not None:
        source: Mapping[str, str] = env
    else:
        dotenv_path = _override_path(os.environ, DOTENV_PATH_ENV) or (_REPO_ROOT / ".env")
        merged = {k: v for k, v in dotenv_values(dotenv_path).items() if v is not None}
        merged.update(os.environ)
        source = merged
    return {key: source[key] for key in ENV_KEYS if source.get(key) not in (None, "")}


def _coerce_int(key: str, raw: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _build_jobs(raw_jobs: Mapping[str, Any], default_model: str) -> dict[str, JobConfig]:
    jobs: dict[str, JobConfig] = {}
    for name, raw in raw_jobs.items():
        if not isinstance(raw, Mapping):
            raise ConfigError(f"[jobs.{name}] must be a table, got {type(raw).__name__}")
        data = dict(raw)
        data.setdefault("model", default_model)
        try:
            jobs[name] = JobConfig(name=name, **data)
        except Exception as exc:  # pydantic ValidationError, or a bad key
            raise ConfigError(f"[jobs.{name}] is invalid: {exc}") from exc
    return jobs


def load_settings(
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Read ``config.toml`` and overlay the environment into a frozen Settings.

    ``config_path`` defaults to ``$HAL_MARY_CONFIG``, then ``config.toml`` at the
    repo root, then the working directory. ``env`` defaults to
    ``$HAL_MARY_ENV`` (or the repo-root ``.env``) overlaid by ``os.environ``;
    when a mapping is passed it is used verbatim and no file is read.
    """
    override_source: Mapping[str, str] = env if env is not None else os.environ
    path = (
        Path(config_path) if config_path is not None else _resolve_config_path(override_source)
    )
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    try:
        claude = ClaudeConfig(**raw.get("claude", {}))
        paths = PathsConfig(**raw.get("paths", {}))
        draft = DraftConfig(**raw.get("draft", {}))
        espn = EspnConfig(**raw.get("espn", {}))
        web = WebConfig(**raw.get("web", {}))
    except Exception as exc:
        raise ConfigError(f"{path} is missing or has an invalid section: {exc}") from exc

    jobs = _build_jobs(raw.get("jobs", {}), claude.default_model)

    values = _resolve_env(env)
    for key in INT_ENV_KEYS:
        if key in values:
            values[key] = _coerce_int(key, values[key])

    return Settings(
        claude=claude,
        paths=paths,
        draft=draft,
        espn=espn,
        web=web,
        jobs=jobs,
        espn_s2=values.get("ESPN_S2"),
        swid=values.get("SWID"),
        league_id=values.get("LEAGUE_ID"),
        team_id=values.get("TEAM_ID"),
        season=values.get("SEASON"),
        web_password=values.get("WEB_PASSWORD"),
        db_path=values.get("DB_PATH", DEFAULT_DB_PATH),
    )
