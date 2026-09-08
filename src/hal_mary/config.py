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

Every path is anchored to config.toml, not to the working directory
--------------------------------------------------------------------
``paths.prompts_dir``, ``paths.memory_dir``, ``claude.scratch_dir``,
``claude.system_prompt_file`` and ``DB_PATH`` are resolved **once, here**,
against the directory holding the resolved ``config.toml``. Absolute values are
passed through untouched.

That anchor is the only one that is right in a developer's checkout, under the
systemd unit (whose ``WorkingDirectory`` is not the source tree) and under any
future packaging. The alternative — every consumer calling ``Path(value)`` and
getting the process working directory — is not a crash: ``standing_memory()``
finds no directory, returns ``""``, and every prompt goes out missing the
standing context that says who Caroline is and what the league's rules are. The
service looks healthy and the advice quietly gets worse.

So :class:`Settings` exposes resolved, absolute ``Path`` objects rather than
strings each caller re-resolves its own way. Consumers must use them as given.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "ActionsConfig",

    "BackupConfig",
    "ChatConfig",
    "ClaudeConfig",
    "ConfigError",
    "CoworkConfig",
    "DraftConfig",
    "EspnConfig",
    "JobConfig",
    "LeagueConfig",
    "PathsConfig",
    "ResearchConfig",
    "SchedulerConfig",
    "Settings",
    "WebConfig",
    "load_settings",
]

#: Environment keys read into :class:`Settings`.
ENV_KEYS = (
    "ESPN_S2",
    "SWID",
    "LEAGUE_ID",
    "TEAM_ID",
    "SEASON",
    "WEB_PASSWORD",
    "MCP_TOKEN",
    "DB_PATH",
)

#: Keys that must be present before hal-mary can talk to ESPN or serve the web app.
#: ``DB_PATH`` is absent on purpose: it has a default. So is ``MCP_TOKEN``: the
#: MCP endpoint is opt-in, and without it ``/mcp`` refuses to serve rather than
#: serving openly, which is a working deployment and not a missing secret.
REQUIRED_ENV_KEYS = ("ESPN_S2", "SWID", "LEAGUE_ID", "TEAM_ID", "SEASON", "WEB_PASSWORD")

#: Env keys whose values are integers.
INT_ENV_KEYS = ("LEAGUE_ID", "TEAM_ID", "SEASON")

#: Deployment overrides. These are paths, not secrets, and are read from the
#: process environment before anything else. The systemd unit sets them because
#: its WorkingDirectory is not the source tree, so neither the repo-root nor the
#: cwd guess below finds the right files.
CONFIG_PATH_ENV = "HAL_MARY_CONFIG"
DOTENV_PATH_ENV = "HAL_MARY_ENV"

#: Where the database goes when ``DB_PATH`` is unset — and it *is* unset on any
#: box set up in a hurry, because ``.env.example`` ships ``DB_PATH=`` empty.
#:
#: Not ``./hal.db``. Since every configured path is anchored to the directory
#: holding ``config.toml``, a relative default lands **inside the checkout** —
#: the one directory a deploy replaces and a rollback moves, and the directory
#: the backups then follow the database into. ``~`` is expanded by
#: :func:`_anchor`, so this is absolute by the time any Settings exists.
DEFAULT_DB_PATH = "~/hal-mary-data/hal.db"

#: Config keys that name a path and are anchored to the config file's directory,
#: keyed by the section attribute they live on. Adding a path to config.toml
#: means adding it here; nothing else has to change.
#:
#: ``claude.binary`` is deliberately **not** here and must not be added. It is a
#: command name, not a path: the shipped value is a bare ``"claude"``, which has
#: to reach ``subprocess`` unchanged so it is looked up on ``PATH``. Anchoring it
#: would turn that into ``<config dir>/claude`` and break every call. The cost is
#: that a *relative* binary path like ``"./bin/claude"`` still resolves against
#: the working directory — write it absolute if you need to point at one.
ANCHORED_PATHS: dict[str, tuple[str, ...]] = {
    "claude": ("scratch_dir", "system_prompt_file"),
    "paths": ("prompts_dir", "memory_dir", "cowork_tasks"),
    "backup": ("dir",),
}

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
    #: Anchored to the config file's directory by :class:`Settings`. Absolute
    #: once a Settings exists; never resolve it again.
    scratch_dir: Path
    system_prompt_file: Path


class ChatConfig(_Frozen):
    """How much context the chat page puts in front of the model.

    Every field is defaulted so a ``config.toml`` written before this section
    existed still loads — the chat page is not draft-critical and must never be
    the reason the draft page will not start.
    """

    #: Notes retrieved for one question. Bigger than the draft's because a chat
    #: question has no pick clock, and retrieval is the only thing standing
    #: between a question about a player and an answer from training data.
    note_limit: int = 25
    #: Recent recommendations shown to the model and down the side of the page.
    advice_limit: int = 5
    #: Conversations listed on the page.
    session_limit: int = 20


class PathsConfig(_Frozen):
    #: All anchored to the config file's directory by :class:`Settings`.
    prompts_dir: Path
    memory_dir: Path
    #: The declarative Cowork task file. A default is supplied so a config.toml
    #: written before the MCP endpoint existed still loads.
    cowork_tasks: Path = Path("cowork/tasks.toml")


class DraftConfig(_Frozen):
    """How the draft loop behaves, and how much of the board each prompt sees.

    The sizes are here rather than in the code because they are the dial between
    a prompt that is too thin to reason from and one that will not come back
    inside the pick clock. Defaults are supplied so an older ``config.toml``
    without them still loads.
    """

    poll_seconds: int
    #: What the loop waits between polls when no draft is running. The draft is
    #: one evening; ``poll_seconds`` around the clock is three orders of
    #: magnitude more requests than the job needs. Defaulted so an older
    #: ``config.toml`` without it still loads — at the value the shipped file
    #: carries, not at ``poll_seconds``, because a default that quietly restored
    #: the five-second forever loop would hide exactly the bug it replaces.
    idle_poll_seconds: int = 300
    #: How long "The draft has started" pins the loop to ``poll_seconds`` while
    #: ESPN's board still shows no picks. It has to outlive the gap between the
    #: draft opening and pick 1; it expires so a stray tap costs an hour rather
    #: than the rest of the season.
    live_override_seconds: int = 3600
    advise_within_picks: int
    #: How many players the pre-draft research job is asked to rank.
    board_size: int = 200
    #: The largest share of the board any one website may be the deciding
    #: source for. A dial rather than a constant because it is the difference
    #: between a board that is research and a board that is one outlet's
    #: ranking retyped: the first build cited a single article for 103 of 200
    #: players, which is a board every other manager in the league can already
    #: see. ``board_build`` renders it as a player count, because a fraction is
    #: something the model would have to multiply out before it could obey it.
    max_source_share: float = 0.25
    #: Notes retrieved into the (slow, pre-draft) research prompt.
    research_note_limit: int = 30
    #: Board rows shown to the advisor on the clock, and on its shorter retry.
    advice_candidates: int = 14
    advice_retry_candidates: int = 5
    #: Recent picks shown to the advisor, and notes retrieved for its candidates.
    advice_recent_picks: int = 8
    advice_note_limit: int = 12
    #: Wall-clock seconds one draft-loop tick may spend before Caroline has a
    #: card, counting the ESPN sync and every Claude attempt. The advisor starts
    #: an attempt only if it can finish inside what is left, so the tick is
    #: bounded by construction rather than by arithmetic in a comment.
    advice_budget_s: int = 60


class ResearchConfig(_Frozen):
    """How much live state the four in-season research jobs put in a prompt.

    Sizes, not cadences: the cadences are the ``cron`` strings on each
    ``[jobs.*]`` entry. Every value is defaulted so an older ``config.toml``
    without a ``[research]`` section still loads.
    """

    #: How many free agents to ask ESPN for. ESPN returns them most-owned first,
    #: so this is "how deep into the wire to look" — and in a six-team league the
    #: interesting names are further down that list than a twelve-team guide
    #: would suggest, because far more good players go unowned.
    free_agent_size: int = 60
    #: How many of them reach the prompt. Smaller than the pull on purpose: the
    #: extra rows exist so the job can filter before spending tokens on them.
    free_agent_shortlist: int = 40
    #: Stored notes retrieved into an in-season prompt.
    note_limit: int = 30
    #: How long a note written by the news sweep stays true, in days. An injury
    #: report is worth a great deal on Wednesday and is actively misleading three
    #: weeks later, so these expire rather than waiting to be pruned by age.
    note_shelf_life_days: int = 14
    #: How many waiver claims to rank. Past about five she is reading a list
    #: rather than making a decision.
    waiver_claims: int = 5


class SchedulerConfig(_Frozen):
    """When hal-mary thinks it is, and how often it re-checks.

    The phase decides which jobs are on the schedule at all — a board build every
    morning before the draft, a lineup check every Sunday after it — so these
    windows are what let one long-running process cross draft night without being
    restarted by hand.
    """

    #: How long before the draft's start time counts as "the draft is happening".
    #: Generous, because ESPN draft times slip and the draft page is worth having
    #: in fast mode early rather than late.
    draft_window_before_hours: float = 6.0
    #: And after. Long enough to cover a sixteen-round draft that stalls.
    draft_window_after_hours: float = 12.0
    #: How long the season lasts from the draft. After it, the off-season: no
    #: research jobs run, and nothing has to be turned off by hand.
    season_days: int = 150
    #: When to re-evaluate the phase. Daily, in the small hours, so the process
    #: moves from pre-draft to in-season on its own rather than at a restart.
    phase_cron: str = "20 4 * * *"
    #: The timezone every cadence is read in. **Not UTC**, and this is not a
    #: preference: the jobs are timed against NFL kickoffs, so "Sunday morning"
    #: in UTC is 2am Pacific — before the Sunday inactive lists the lineup
    #: prompt is told to go and read, and a whole day off once the clocks change.
    timezone: str = "America/Los_Angeles"
    #: How late a fire may be and still run. APScheduler's default is one
    #: second, so a fire missed while the loop was blocked — a long sync, a
    #: suspended box — is dropped, and the only trace is a log line nobody
    #: reads. An hour late is still worth having for every job here.
    misfire_grace_time_s: int = 3600


class EspnConfig(_Frozen):
    """Transport limits for the raw ESPN reads.

    These belong here rather than in the client because they are bounded by the
    thing configured immediately above them: the pick clock is 90 seconds
    and ``draft.poll_seconds`` is 5, so a read that outlives its poll silently
    stops the draft loop. Defaults are supplied so an older ``config.toml``
    without an ``[espn]`` section still loads.
    """

    connect_timeout_s: float = 10.0
    read_timeout_s: float = 15.0


class LeagueConfig(_Frozen):
    """The manual fallback for the league's own settings.

    Normally ``hal-mary sync`` writes the ``league_settings`` table from ESPN and
    nothing here is read. This section exists for the case that decides whether
    hal-mary is useful on draft night at all: **no working ESPN credentials.**
    Pick discovery already has a manual path; without this, the league's size,
    scoring and draft order would have none, and every recommendation would be
    built on a guessed twelve-team standard-scoring default.

    Everything is optional, and a synced row always wins field by field, so a
    partly-filled section is still worth having. ``hal_mary.league`` applies the
    precedence; nothing else reads this.

    ``draft_order`` is team ids or team names by first-round slot. Names are for
    the realistic case: Caroline reading the ESPN draft lobby, which shows names
    and no ids. When names are given, a team's id *is* its 1-based slot.
    """

    team_count: int | None = None
    scoring_type: str | None = None
    points_per_reception: float | None = None
    draft_type: str | None = None
    draft_date: str | None = None
    name: str | None = None
    rounds: int | None = None
    #: Seconds on the clock per pick. Every draft-night budget is sized against
    #: it, so it is worth being able to state by hand when ESPN is unavailable.
    pick_clock_s: int | None = None
    #: What the season is a race for. How many teams reach the playoffs, how
    #: the seeds are ordered (ESPN's own wording, e.g. ``TOTAL_POINTS_SCORED``)
    #: and how many weeks the regular season runs. A league where most teams
    #: get in and the seeds go by points scored rewards a different board from
    #: one where a quarter get in on win-loss record, so it is stated rather
    #: than assumed — and stateable by hand, because every other league fact is.
    playoff_team_count: int | None = None
    playoff_seeding_rule: str | None = None
    regular_season_weeks: int | None = None
    my_draft_slot: int | None = None
    draft_order: list[int | str] = []
    roster_slots: dict[str, int] = {}


class ActionsConfig(_Frozen):
    """When an NFL week rolls over, for the purpose of expiring an instruction.

    Two things depend on this one boundary, which is why it is a configured value
    rather than a number in the code.

    **Every emitted action expires at it.** A lineup change is worthless once the
    week it was reasoned about is over, and an executor that has been offline for
    a fortnight must not come back and perform it. Without a deadline there is no
    expiry and no other revocation path, so this is what makes "actions expire"
    a mechanism instead of a sentence in a design document.

    **Emission equivalence is scoped by it.** "The same bench, already queued this
    week" is a duplicate; the same bench next week is a new decision — he was on
    bye then and is injured now — and suppressing it would lose a real change.

    The defaults put the boundary after Monday night's game (which ends around
    04:15 UTC on Tuesday) and around when ESPN rolls its scoring period. UTC
    because that is the only clock the database stores.
    """

    week_boundary_weekday: str = "tuesday"
    week_boundary_hour_utc: int = 11


class CoworkConfig(_Frozen):
    """How the Cowork schedule is rendered.

    ``timezone`` is the zone Cowork's own scheduling form uses — the operator's
    local zone, not the server's. It is here rather than read from the box
    because the box runs UTC and the person filling in the form does not, and a
    bare hour with no zone beside it is the kind of thing that schedules a lineup
    run six hours after kickoff.
    """

    timezone: str = "UTC"
    #: How long before waiver processing a claim run should happen. ESPN
    #: processes claims in a batch, so a run after that time is worth nothing;
    #: a day ahead leaves room for a failed run to be noticed.
    waiver_lead_minutes: int = 1440

class BackupConfig(_Frozen):
    """The nightly snapshot of the database.

    ``dir`` is optional and unset by default, which means "beside the database"
    — see :meth:`Settings.backup_dir`. A configured value is anchored to
    ``config.toml`` like every other path here, so setting it to something inside
    the checkout is possible and is a mistake: a deploy replaces a checkout, and
    surviving that is the whole point of a backup.

    ``keep`` is a count of files, not days, because the timer may miss a night
    (the box was off) and "the last fourteen backups" is the window someone
    actually reasons about when restoring.
    """

    dir: Path | None = None
    keep: int = 14


class WebConfig(_Frozen):
    """How the web app listens, signs sessions and paces its background work.

    The three tunables below carry defaults so a ``config.toml`` written before
    the web app existed still loads. They live here rather than in the code
    because they are exactly what CLAUDE.md rule 5 is about: a session lifetime
    that logs Caroline out mid-draft, a heartbeat too slow for a phone's proxy,
    or an ESPN auth check hammering an unofficial endpoint are all things to
    change in config on the box, not to ship as code.
    """

    host: str
    port: int
    session_cookie: str
    #: The double-submit CSRF cookie. Named apart from the session cookie
    #: because they answer different questions: the session says who she is,
    #: this one says the form came from a page hal-mary itself served.
    csrf_cookie: str = "hal_mary_csrf"
    #: How long a login lasts. Long by design: being logged out with a
    #: 90-second pick clock running is worse than the risk on a home LAN.
    session_max_age_days: int = 30
    #: How far behind the draft sync may fall before the draft page bands
    #: itself with a warning. A cadence, not a constant: it is sized against
    #: ``draft.poll_seconds`` and the league's pick clock, both of which live
    #: in this file, and what is right for a 90-second clock is wrong for a
    #: 30-second one.
    draft_stale_seconds: int = 30
    #: How often the draft page re-fetches its live fragment once the event
    #: stream has dropped. Milliseconds, because that is what it is handed to.
    live_poll_ms: int = 10000
    #: Comment-frame interval on ``/events``. Phone browsers and anything
    #: between them and the box drop a silent stream; this keeps it open.
    sse_heartbeat_s: float = 15.0
    #: How often the status page re-checks the ESPN cookies. It is a network
    #: call on the page most likely to be reloaded when something looks wrong.
    auth_check_seconds: int = 3600
    #: Failed logins from one client before it is locked out, and for how long.
    #: The only thing between a device on the network and guessing a household
    #: password a few thousand times a second.
    login_max_attempts: int = 5
    login_lockout_seconds: float = 60.0
    #: Seconds uvicorn may spend waiting for open connections on the way down.
    #: Bounded because ``/events`` never ends on its own and lifespan shutdown —
    #: which stops the draft loop — runs only once uvicorn stops waiting.
    #: Defaulted so an older ``config.toml`` without it still loads.
    shutdown_timeout_s: int = 5
    #: Which upstream addresses may set ``X-Forwarded-Proto`` / ``-For``.
    #:
    #: Empty means "nothing is in front of us", which is the LAN default. Behind
    #: the homelab's Traefik it must name the proxy, because uvicorn only trusts
    #: 127.0.0.1 otherwise and *silently ignores* the headers: the app then sees
    #: scheme "http" on an https site and sets the session cookie **without**
    #: Secure, so a cookie for an https origin becomes sendable over plaintext.
    #: The page still works, which is why this has to be a setting somebody can
    #: see rather than something noticed the day it matters. "*" is accepted and
    #: is only safe when nothing but the proxy can reach the port.
    forwarded_allow_ips: str = ""



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
    #: When it runs. One crontab string, or several — a job can have more than
    #: one cadence, and the lineup check does: ESPN locks each player at his own
    #: kickoff rather than once a week, so Thursday and Monday night games need
    #: their own check and cannot share Sunday morning's hour.
    #:
    #: **Name the weekday, never number it.** APScheduler's ``from_crontab``
    #: counts weekdays from Monday and crontab(5) counts from Sunday, so a digit
    #: in that field is right only by accident; ``0 9 * * 0`` fires on Monday.
    #: ``tests/unit/test_scheduler.py`` refuses a digit there.
    cron: str | list[str] | None = None

    @property
    def crons(self) -> list[str]:
        """Every cadence this job has, as a list. Empty means "on demand only"."""
        if not self.cron:
            return []
        if isinstance(self.cron, str):
            return [self.cron]
        return [entry for entry in self.cron if entry]

    @property
    def cadence(self) -> str:
        """The cadences as one line, for the CLI and the status page."""
        return ", ".join(self.crons)


class Settings(_Frozen):
    """The whole resolved configuration: config.toml plus the environment.

    Every path field is absolute by the time an instance exists — see
    :meth:`_anchor_paths` and the module docstring. Consumers use them as given.
    """

    #: The file this was loaded from. Every relative path in it is resolved
    #: against ``config_path.parent``. The default keeps a directly-constructed
    #: Settings (tests, mostly) behaving like one loaded from the checkout.
    config_path: Path = _REPO_ROOT / "config.toml"

    claude: ClaudeConfig
    paths: PathsConfig
    chat: ChatConfig = ChatConfig()
    draft: DraftConfig
    espn: EspnConfig = EspnConfig()
    league: LeagueConfig = LeagueConfig()
    actions: ActionsConfig = ActionsConfig()
    cowork: CoworkConfig = CoworkConfig()
    backup: BackupConfig = BackupConfig()
    research: ResearchConfig = ResearchConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    web: WebConfig
    jobs: dict[str, JobConfig]

    espn_s2: str | None = None
    swid: str | None = None
    league_id: int | None = None
    team_id: int | None = None
    season: int | None = None
    web_password: str | None = None
    #: The bearer token for ``/mcp``, deliberately separate from
    #: ``web_password``. Two doors, two keys: the MCP endpoint is what a tunnel
    #: exposes to the internet, the dashboard is LAN-only, and one shared
    #: credential would put Caroline's ESPN session cookies on the public side.
    #: ``None`` means ``/mcp`` refuses every request; absent never means open.
    mcp_token: str | None = None
    db_path: Path = Path(DEFAULT_DB_PATH)

    @model_validator(mode="after")
    def _anchor_paths(self) -> Settings:
        """Resolve every configured path against the config file's directory.

        Done here rather than in ``load_settings`` so that *no* route to a
        Settings — a direct construction in a test, a ``model_validate``, a
        future loader — can produce one carrying a working-directory-relative
        path. Absolute values pass through exactly as given.

        ``config_path`` is itself resolved first, and that ``.resolve()`` is what
        makes the promise above true rather than nearly true. ``load_settings``
        always hands over an absolute path, but ``config_path`` is a public
        field: a relative one would anchor every other path to a relative root,
        which is the original bug wearing the guard's uniform.
        """
        root = self.config_path.expanduser().resolve().parent
        object.__setattr__(self, "config_path", self.config_path.expanduser().resolve())
        for section, fields in ANCHORED_PATHS.items():
            current = getattr(self, section)
            object.__setattr__(
                self,
                section,
                current.model_copy(
                    update={f: _anchor(getattr(current, f), root) for f in fields}
                ),
            )
        object.__setattr__(self, "db_path", _anchor(self.db_path, root))
        return self

    def backup_dir(self) -> Path:
        """Where ``hal-mary backup`` writes, resolved.

        Defaults to ``backups/`` beside the database rather than beside
        ``config.toml``, because the database is on the box's own disk and the
        checkout is the thing a deploy replaces.
        """
        return self.backup.dir or self.db_path.parent / "backups"

    def resolved_paths(self) -> list[tuple[str, Path, bool]]:
        """Every configured path, resolved, with whether it exists on disk.

        The status page renders this. An operator seeing "Memory · /srv/hal/memory
        · missing" can tell the difference between "there are no notes" and "the
        service is looking in the wrong place" without an SSH session — which is
        the whole failure this anchoring exists to make visible.
        """
        return [
            (label, path, path.exists())
            for label, path in (
                ("Config file", self.config_path),
                ("Memory", self.paths.memory_dir),
                ("Prompts", self.paths.prompts_dir),
                ("System prompt", self.claude.system_prompt_file),
                ("Claude scratch", self.claude.scratch_dir),
                ("Cowork tasks", self.paths.cowork_tasks),
                ("Database", self.db_path),
            )
        ]

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


def _anchor(value: str | Path | None, root: Path) -> Path | None:
    """``value`` as an absolute path, relative ones resolved against ``root``.

    ``None`` passes through: an optional path that was not configured has no
    anchor to resolve against, and inventing one here would silently give
    ``backup.dir`` a value nobody asked for.
    """
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


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

    Every configured path in the result is absolute, anchored to the directory
    holding the file that was actually read — never the working directory.
    """
    override_source: Mapping[str, str] = env if env is not None else os.environ
    path = (
        Path(config_path) if config_path is not None else _resolve_config_path(override_source)
    )
    # Absolute from here on: it is the anchor for every configured path, and an
    # anchor that is itself relative to the working directory anchors nothing.
    path = path.expanduser().resolve()
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
        chat = ChatConfig(**raw.get("chat", {}))
        draft = DraftConfig(**raw.get("draft", {}))
        espn = EspnConfig(**raw.get("espn", {}))
        league = LeagueConfig(**raw.get("league", {}))
        actions = ActionsConfig(**raw.get("actions", {}))
        cowork = CoworkConfig(**raw.get("cowork", {}))
        backup = BackupConfig(**raw.get("backup", {}))
        research = ResearchConfig(**raw.get("research", {}))
        scheduler = SchedulerConfig(**raw.get("scheduler", {}))
        web = WebConfig(**raw.get("web", {}))
    except Exception as exc:
        raise ConfigError(f"{path} is missing or has an invalid section: {exc}") from exc

    jobs = _build_jobs(raw.get("jobs", {}), claude.default_model)

    values = _resolve_env(env)
    for key in INT_ENV_KEYS:
        if key in values:
            values[key] = _coerce_int(key, values[key])

    return Settings(
        config_path=path,
        claude=claude,
        paths=paths,
        chat=chat,
        draft=draft,
        espn=espn,
        league=league,
        actions=actions,
        cowork=cowork,
        backup=backup,
        research=research,
        scheduler=scheduler,
        web=web,
        jobs=jobs,
        espn_s2=values.get("ESPN_S2"),
        swid=values.get("SWID"),
        league_id=values.get("LEAGUE_ID"),
        team_id=values.get("TEAM_ID"),
        season=values.get("SEASON"),
        web_password=values.get("WEB_PASSWORD"),
        mcp_token=values.get("MCP_TOKEN"),
        db_path=values.get("DB_PATH", DEFAULT_DB_PATH),
    )
