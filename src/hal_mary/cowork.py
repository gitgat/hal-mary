"""Claude Cowork's scheduled tasks: the configuration, and the renderer for it.

A Cowork scheduled task is a saved prompt on a cadence. **The prompt is the cron
job**, so the prompts are a shipped artifact rather than documentation, and they
live in ``cowork/tasks.toml`` as data — adding a job is an edit to that file, not
a code change.

**Every saved prompt is static and generic.** It never names a player, a week or
a strategy. It says: ask hal-mary what to do, do exactly that, report back. All
the intelligence stays on hal-mary's side of the boundary, which is the whole
point of the split — and it means a prompt saved in September is still correct in
December without anyone editing it.

Two things here are structural rather than advisory:

* **``mode = "read_only"`` cannot act.** A read-only job's tool list may not
  contain an acting tool, and :func:`load_tasks` refuses a file that breaks that.
  The session that reads other league members' page text is the one exposed to
  prompt injection; giving it no way to change the roster is what makes an
  injected instruction inert rather than merely unlikely to be followed.
* **The schedule is derived from this league.** A waiver claim is submitted into
  a batch that ESPN processes at a time the league sets, so a claim run timed
  after that is worth nothing. The waiver job's time comes from
  ``acquisitionSettings`` in the synced league payload. When that is absent the
  renderer says so loudly rather than assuming ESPN's Wednesday default — a
  confident wrong time is worse than a blank one.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hal_mary.config import Settings

__all__ = [
    "ACTING_TOOLS",
    "CADENCES",
    "KNOWN_TOOLS",
    "MODES",
    "CoworkConfigError",
    "Schedule",
    "ScheduledTask",
    "Task",
    "as_json",
    "load_tasks",
    "render",
    "render_text",
    "schedule_payload",
    "waiver_settings",
]


class CoworkConfigError(ValueError):
    """Raised when ``cowork/tasks.toml`` cannot be turned into a schedule."""


#: What Cowork's own scheduling form accepts.
CADENCES = ("hourly", "daily", "weekdays", "weekly", "manual")

MODES = ("execute", "read_only")

#: Every tool the MCP server offers. A task may not list a name that is not here,
#: because a saved prompt referring to a tool that does not exist is a job that
#: fails silently at three in the morning.
KNOWN_TOOLS = (
    "get_roster",
    "get_board",
    "get_advice",
    "get_league",
    "pending_actions",
    "report_action",
    "report_observation",
    "cowork_schedule",
)

#: Tools a ``read_only`` task may not list.
#:
#: ``report_action`` because it changes hal-mary's state. ``pending_actions``
#: because fetching the instruction list is part of acting: a read-only job that
#: could fetch it but not report on it would receive a plan, have no way to say
#: what happened to it, and leave hal-mary re-issuing every entry forever.
#:
#: ``report_observation`` is deliberately not here. It only ever writes a note
#: that ``memory.build_context`` quarantines into its own untrusted section, so
#: it cannot put anything in front of a later prompt as an established fact.
#:
#: **This bounds the tools hal-mary hands out, and nothing more.** It says
#: nothing about the browser the same Cowork session is holding, which is logged
#: into ESPN and could in principle change the roster by clicking. That is why
#: the read-only prompts say hal-mary has given them no tool that changes
#: anything, rather than claiming they are incapable of it.
ACTING_TOOLS = frozenset({"report_action", "pending_actions"})

DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

#: Derivations a task may ask for instead of stating its own time.
DERIVATIONS = ("waivers",)


@dataclass(frozen=True)
class Task:
    """One Cowork scheduled task, exactly as the file declares it."""

    name: str
    purpose: str
    enabled: bool
    cadence: str
    model: str
    mode: str
    tools: tuple[str, ...]
    prompt: str
    at: str | None = None
    day: str | None = None
    derive: str | None = None
    lead_minutes: int | None = None


@dataclass(frozen=True)
class ScheduledTask:
    """One task with its time resolved against this league."""

    task: Task
    day: str | None
    at: str | None
    next_run: str | None
    notes: tuple[str, ...]
    #: For the waiver run: the weekday whose processing batch it is aiming at.
    #: ``None`` for every other task, and when the league's waiver settings are
    #: unknown. It is not cosmetic — it is what makes "before processing" and
    #: "after the scan" two separate checks instead of one fuzzy one.
    targets_processing_on: str | None = None


@dataclass(frozen=True)
class Schedule:
    """Every task, resolved, plus whatever could not be resolved and why."""

    timezone: str
    tasks: tuple[ScheduledTask, ...]
    warnings: tuple[str, ...]
    waivers: dict[str, Any]


# --- loading -----------------------------------------------------------------


def tasks_path(settings: Settings) -> Path:
    """The task file, already absolute.

    Returned as given, not re-wrapped: ``Settings`` anchors every configured path
    to the config file's directory, and a ``Path(...)`` around one here would
    read as a safety net while being the bug that anchoring exists to prevent —
    it would silently accept a working-directory-relative value from some future
    caller that skipped the anchoring.
    """
    return settings.paths.cowork_tasks


def _require(raw: dict[str, Any], key: str, name: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise CoworkConfigError(f"task {name!r} is missing {key!r}")
    return raw[key]


def _valid_time(value: str, name: str) -> str:
    """Normalise a wall-clock ``HH:MM``. No date and no zone: it is a form field."""
    raw_hour, _, raw_minute = str(value).partition(":")
    try:
        hour, minute = int(raw_hour), int(raw_minute)
    except ValueError:
        raise CoworkConfigError(
            f"task {name!r} has an unreadable time {value!r}; use 24-hour HH:MM"
        ) from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise CoworkConfigError(
            f"task {name!r} has an impossible time {value!r}; use 24-hour HH:MM"
        )
    return f"{hour:02d}:{minute:02d}"


def _build_task(raw: Any, default_model: str) -> Task:
    if not isinstance(raw, dict):
        raise CoworkConfigError(f"every [[task]] must be a table, got {type(raw).__name__}")
    name = str(_require(raw, "name", "(unnamed)")).strip()

    cadence = str(_require(raw, "cadence", name)).lower()
    if cadence not in CADENCES:
        raise CoworkConfigError(
            f"task {name!r} has cadence {cadence!r}; Cowork accepts {', '.join(CADENCES)}"
        )

    mode = str(raw.get("mode", "execute")).lower()
    if mode not in MODES:
        raise CoworkConfigError(f"task {name!r} has mode {mode!r}; expected one of {MODES}")

    tools = tuple(str(tool) for tool in raw.get("tools", ()))
    unknown = [tool for tool in tools if tool not in KNOWN_TOOLS]
    if unknown:
        raise CoworkConfigError(
            f"task {name!r} lists tool(s) the MCP server does not offer: {', '.join(unknown)}"
        )
    if mode == "read_only":
        acting = sorted(set(tools) & ACTING_TOOLS)
        if acting:
            raise CoworkConfigError(
                f"task {name!r} is mode=read_only but lists the acting tool(s) "
                f"{', '.join(acting)}. A read-only job reads pages other people wrote; "
                "giving it a way to act is the boundary this design exists to hold."
            )

    prompt = str(raw.get("prompt", "")).strip()
    if not prompt:
        raise CoworkConfigError(f"task {name!r} has no prompt; the prompt is the job")

    derive = raw.get("derive")
    if derive is not None:
        derive = str(derive).lower()
        if derive not in DERIVATIONS:
            raise CoworkConfigError(
                f"task {name!r} asks for an unknown derivation {derive!r}; "
                f"expected one of {', '.join(DERIVATIONS)}"
            )

    day = raw.get("day")
    if day is not None:
        day = str(day).lower()
        if day not in DAYS:
            raise CoworkConfigError(f"task {name!r} has day {day!r}; expected a weekday name")
    at = raw.get("at")
    at = _valid_time(at, name) if at else None

    if cadence == "weekly" and derive is None and not day:
        raise CoworkConfigError(f"task {name!r} is weekly but names no day")
    if cadence in ("daily", "weekdays", "weekly") and derive is None and not at:
        raise CoworkConfigError(f"task {name!r} is {cadence} but names no time")

    return Task(
        name=name,
        purpose=str(raw.get("purpose", "")).strip(),
        enabled=bool(raw.get("enabled", False)),
        cadence=cadence,
        model=str(raw.get("model") or default_model),
        mode=mode,
        tools=tools,
        prompt=prompt,
        at=at,
        day=day,
        derive=derive,
        lead_minutes=(int(raw["lead_minutes"]) if raw.get("lead_minutes") is not None else None),
    )


def load_tasks(settings: Settings, path: str | Path | None = None) -> tuple[Task, ...]:
    """Read and validate every ``[[task]]`` in the Cowork task file.

    Validation is deliberately strict and fails the whole file. A schedule half
    of which is wrong is a schedule nobody can trust, and every failure here is
    something a person can fix in one line.
    """
    file_path = Path(path) if path is not None else tasks_path(settings)
    try:
        raw = tomllib.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CoworkConfigError(
            f"{file_path} not found; it is the source of Cowork's scheduled prompts"
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise CoworkConfigError(f"{file_path} is not valid TOML: {exc}") from exc

    entries = raw.get("task", [])
    if not isinstance(entries, list):
        raise CoworkConfigError(f"{file_path} must declare jobs as [[task]] tables")

    tasks = tuple(_build_task(entry, settings.claude.default_model) for entry in entries)
    seen: set[str] = set()
    for task in tasks:
        if task.name in seen:
            raise CoworkConfigError(f"{file_path} declares two tasks named {task.name!r}")
        seen.add(task.name)
    return tasks


# --- the league's own numbers ------------------------------------------------


def waiver_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """When this league processes waiver claims, from the synced ESPN payload.

    Returns ``{"known": False}`` when nothing has synced or the payload does not
    carry it. Never guesses: a claim run scheduled after processing submits
    nothing, and "Wednesday because that is ESPN's default" is exactly the kind
    of confident wrong answer that goes unnoticed for a season.
    """
    row = conn.execute("SELECT raw_json FROM league_settings WHERE id = 1").fetchone()
    if row is None or not row["raw_json"]:
        return {"known": False, "reason": "nothing has synced from ESPN yet"}
    try:
        raw = json.loads(row["raw_json"])
    except (TypeError, ValueError):  # pragma: no cover - only a hand-edited row
        return {"known": False, "reason": "the synced league payload is not readable"}
    acquisition = (raw or {}).get("acquisitionSettings") or {}
    days = acquisition.get("waiverProcessDays") or []
    # `waiverProcessHour` is the hour claims are processed. `waiverHours` is a
    # different number entirely — how long a player sits on waivers before he
    # clears — and this league sets it to 24, which is not an hour of any day.
    # Reading the second as the first made the run undatable here and would have
    # dated it wrongly in any league where it happened to fall under 24.
    hour = acquisition.get("waiverProcessHour")
    if not days or hour is None:
        return {
            "known": False,
            "reason": "the synced league payload carries no waiver processing day",
        }

    # Unreadable is as unknown as absent, and just as loud. Coercing a day we do
    # not recognise into an index default would reinstate exactly the assumed
    # Wednesday this function exists to refuse — and do it silently, which is
    # worse than the gap it was covering, because nothing then says a guess was
    # made.
    named = [str(day).strip().lower() for day in days]
    unknown = [day for day in named if day not in DAYS]
    if unknown:
        return {
            "known": False,
            "reason": f"the synced waiver processing day is {unknown[0]!r}, which is not a weekday",
        }
    try:
        process_hour = int(hour)
    except (TypeError, ValueError):
        return {"known": False, "reason": f"the synced waiver processing hour is {hour!r}"}
    if not 0 <= process_hour < 24:
        return {
            "known": False,
            "reason": f"the synced waiver processing hour is {process_hour}, not an hour of a day",
        }

    return {
        "known": True,
        "process_days": named,
        "process_hour": process_hour,
        "waiver_period_hours": acquisition.get("waiverHours"),
        "acquisition_type": acquisition.get("acquisitionType"),
    }


# --- rendering ---------------------------------------------------------------


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CoworkConfigError(
            f"[cowork].timezone is {name!r}, which is not an IANA timezone name"
        ) from None


WEEK_MINUTES = 7 * 24 * 60

_CRON_DAYS = {
    "mon": "monday",
    "tue": "tuesday",
    "wed": "wednesday",
    "thu": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}


def _scan_minute(settings: Settings) -> int | None:
    """When hal-mary's waiver scan runs, as a minute of the week.

    ``None`` when there is no scan to be ordered against — an on-demand-only
    job, or one whose cadence names several weekdays, where "the scan" is not a
    single moment and the caller must not pretend it is.
    """
    job = settings.jobs.get("waiver_scan")
    if job is None:
        return None
    # A job's cadence is one crontab string or several; both shapes reach here.
    cron = getattr(job, "cron", None)
    crons = [cron] if isinstance(cron, str) else list(cron or [])
    if len(crons) != 1:
        # No cadence, or several: "the scan" is not one moment to order against.
        return None
    fields = crons[0].split()
    if len(fields) != 5:
        return None
    weekday = fields[4].lower()
    if "," in weekday or weekday == "*":
        return None
    day = _CRON_DAYS.get(weekday[:3])
    if day is None or not fields[1].isdigit():
        return None
    return DAYS.index(day) * 1440 + int(fields[1]) * 60


def _derive_waivers(task: Task, settings: Settings, waivers: dict[str, Any]) -> ScheduledTask:
    if not waivers.get("known"):
        return ScheduledTask(
            task=task,
            day=None,
            at=None,
            next_run=None,
            notes=(
                (
                    "hal-mary does not know when this league processes waiver claims "
                    f"({waivers.get('reason', 'unknown')}), so this run has no time yet. "
                    "Run `hal-mary sync`, or read Settings > Acquisitions in ESPN and set "
                    "`day` and `at` on this task by hand."
                ),
            ),
        )
    lead = (
        task.lead_minutes
        if task.lead_minutes is not None
        else settings.cowork.waiver_lead_minutes
    )
    hour = waivers["process_hour"]
    # Validated by waiver_settings, which reports an unrecognised day as unknown
    # rather than letting one reach here to be defaulted.
    days = list(waivers["process_days"])
    runs = sorted(DAYS.index(day) * 1440 + hour * 60 for day in days)

    scan = _scan_minute(settings)
    if scan is None:
        # Nothing to order against: aim at the first processing run of the week
        # and say plainly that the ordering was not checked.
        target = runs[0]
        gap = WEEK_MINUTES
    else:
        # The processing batch this run is for is the next one after the scan
        # that fills its queue. Anything earlier submits an empty queue, which
        # reports success and does nothing.
        ahead = [(((run - scan) % WEEK_MINUTES), run) for run in runs]
        # A batch at the very same minute as the scan is not "after" it: the two
        # would race, and the scan losing means an empty queue.
        ahead = [(delta, run) for delta, run in ahead if delta > 0]
        gap, target = min(ahead) if ahead else (WEEK_MINUTES, runs[0])

    # A lead longer than the window between the scan and the batch would put the
    # run in front of the scan again. Halve it rather than drop it: the run still
    # wants to be early enough that a failure can be noticed and repeated.
    effective = lead
    clamped = False
    if effective >= gap:
        effective = max(gap // 2, 1)
        clamped = True

    submit = (target - effective) % WEEK_MINUTES
    day_name = DAYS[submit // 1440]
    target_day = DAYS[(target % WEEK_MINUTES) // 1440]
    notes = [
        (
            "Derived from this league: ESPN processes claims on "
            f"{', '.join(day.title() for day in days)} at {hour:02d}:00, and a claim "
            f"submitted after that is worth nothing, so this runs "
            f"{effective // 60} hour(s) before the {target_day.title()} batch."
        )
    ]
    if len(days) > 1:
        notes.append(
            f"This league processes on {len(days)} days a week, so a claim can be "
            f"submitted most days. This run targets {target_day.title()} because it is "
            "the batch that follows hal-mary's waiver scan."
        )
    if clamped:
        notes.append(
            f"The configured lead of {lead} minutes was longer than the "
            f"{gap} minutes between the scan and that batch, so it was shortened "
            "to keep the run after the scan that fills its queue."
        )
    if scan is None:
        notes.append(
            "hal-mary's waiver scan has no single weekly time, so this run could "
            "not be ordered against it. Check by hand that the scan runs first."
        )
    return ScheduledTask(
        task=task,
        day=day_name,
        at=f"{(submit % 1440) // 60:02d}:{submit % 60:02d}",
        next_run=None,
        targets_processing_on=target_day,
        notes=tuple(notes),
    )


def _next_run(entry: ScheduledTask, now: datetime, zone: ZoneInfo) -> str | None:
    """The next wall-clock moment this task fires, in the configured zone."""
    task = entry.task
    if task.cadence == "manual" or not task.enabled:
        return None
    local = now.astimezone(zone)
    if task.cadence == "hourly":
        return (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat()
    if entry.at is None:
        return None
    hour, minute = (int(part) for part in entry.at.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(15):
        if candidate > local and _fires_on(task, entry, candidate):
            return candidate.isoformat()
        candidate += timedelta(days=1)
    return None  # pragma: no cover - a fortnight covers every cadence above


def _fires_on(task: Task, entry: ScheduledTask, moment: datetime) -> bool:
    if task.cadence == "daily":
        return True
    if task.cadence == "weekdays":
        return moment.weekday() < 5
    if task.cadence == "weekly":
        return entry.day is not None and DAYS[moment.weekday()] == entry.day
    return False  # pragma: no cover - hourly and manual return earlier


def render(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tasks: tuple[Task, ...] | None = None,
    now: datetime | None = None,
) -> Schedule:
    """Resolve every task against this league and this clock."""
    declared = tasks if tasks is not None else load_tasks(settings)
    zone_name = settings.cowork.timezone
    zone = _zone(zone_name)
    moment = now or datetime.now(UTC)
    waivers = waiver_settings(conn)

    warnings: list[str] = []
    if zone_name == "UTC":
        warnings.append(
            "Times below are in UTC, which is almost certainly not the zone Cowork's "
            "scheduling form uses. Set [cowork].timezone in config.toml to your own "
            "IANA zone (for example America/Chicago) and print this again."
        )
    elif zone_name != settings.scheduler.timezone:
        # Two settings that must agree, in a place the operator is looking. The
        # schedules interleave — hal-mary queues the lineup changes and Cowork
        # performs them — so a drift of a few hours puts the Cowork run in front
        # of the check that fills its queue, and it finds nothing to do.
        warnings.append(
            f"[cowork].timezone is {zone_name} but [scheduler].timezone is "
            f"{settings.scheduler.timezone}. hal-mary's own jobs run in the second "
            "and the times below are in the first, so they are not the sequence "
            "they look like: a lineup run scheduled before the check that fills "
            "its queue performs nothing. Make them the same zone."
        )
    if conn.execute("SELECT 1 FROM league_settings WHERE id = 1").fetchone() is None:
        warnings.append(
            "Nothing has synced from ESPN, so every time here is the file's own default "
            "rather than this league's. Run `hal-mary sync` and print this again."
        )
    if not waivers.get("known"):
        warnings.append(
            "This league's waiver processing day is unknown, so the waiver run has no "
            "time. hal-mary will not assume ESPN's default: check Settings > "
            "Acquisitions in ESPN and set it on the task by hand if a sync cannot."
        )

    resolved: list[ScheduledTask] = []
    for task in declared:
        if task.derive == "waivers":
            entry = _derive_waivers(task, settings, waivers)
        else:
            entry = ScheduledTask(task=task, day=task.day, at=task.at, next_run=None, notes=())
        # `replace` rather than a hand-listed rebuild: this used to name every
        # field, so a field added to ScheduledTask was silently dropped on the
        # way out and the renderer saw a None it had no way to explain.
        resolved.append(replace(entry, next_run=_next_run(entry, moment, zone)))

    return Schedule(
        timezone=zone_name,
        tasks=tuple(resolved),
        warnings=tuple(warnings),
        waivers=waivers,
    )


# --- output forms ------------------------------------------------------------


def as_json(schedule: Schedule) -> dict[str, Any]:
    """The machine form, for ``--json`` and for the ``cowork_schedule`` tool."""
    return {
        "timezone": schedule.timezone,
        "warnings": list(schedule.warnings),
        "waivers": schedule.waivers,
        "tasks": [
            {
                "name": entry.task.name,
                "purpose": entry.task.purpose,
                "enabled": entry.task.enabled,
                "cadence": entry.task.cadence,
                "day": entry.day,
                "at": entry.at,
                "timezone": schedule.timezone,
                "model": entry.task.model,
                "mode": entry.task.mode,
                "tools": list(entry.task.tools),
                "next_run": entry.next_run,
                "notes": list(entry.notes),
                "prompt": entry.task.prompt,
            }
            for entry in schedule.tasks
        ],
    }


def _when(entry: ScheduledTask, timezone: str) -> str:
    if entry.task.cadence == "manual":
        return "on demand"
    if entry.task.cadence == "hourly":
        return f"every hour ({timezone})"
    if entry.at is None:
        return "NOT SET — see the note below"
    day = f"{entry.day.capitalize()} " if entry.day else ""
    if entry.task.cadence == "weekdays":
        day = "Mon-Fri "
    elif entry.task.cadence == "daily":
        day = "every day "
    return f"{day}{entry.at} {timezone}"


def render_text(schedule: Schedule) -> str:
    """The human form: a summary table, then one block per task to paste in.

    The block is field for field what Cowork's setup form asks for, and the
    prompt is the whole prompt rather than a description of it — the point is
    that it can be pasted without editing.
    """
    lines: list[str] = []
    lines.append("Cowork scheduled tasks for this league")
    lines.append("=" * 38)
    lines.append("")
    lines.append(f"Times below are {schedule.timezone}. Cowork's form takes your local time.")
    lines.append("")

    for warning in schedule.warnings:
        lines.append(f"!! {warning}")
    if schedule.warnings:
        lines.append("")

    name_width = max([len(entry.task.name) for entry in schedule.tasks] + [4])
    # Measured, not guessed. "When" carries the zone name, and a real IANA zone
    # ("America/Los_Angeles") is nineteen characters where the old fixed width
    # was sized for "UTC" — which pushed every column after it out of true in
    # the one output whose entire job is to be read by a person.
    whens = [_when(entry, schedule.timezone) for entry in schedule.tasks]
    when_width = max([len(when) for when in whens] + [len("When")])
    next_width = max(
        [len(entry.next_run or "-") for entry in schedule.tasks] + [len("Next run")]
    )
    lines.append(
        f"{'Job'.ljust(name_width)}  {'On':<10} "
        f"{'When'.ljust(when_width)} {'Next run'.ljust(next_width)} Mode"
    )
    lines.append("-" * (name_width + when_width + next_width + 21))
    for entry, when in zip(schedule.tasks, whens, strict=True):
        state = "enabled" if entry.task.enabled else "off"
        lines.append(
            f"{entry.task.name.ljust(name_width)}  {state:<10} "
            f"{when.ljust(when_width)} {(entry.next_run or '-').ljust(next_width)} "
            f"{entry.task.mode}"
        )
    lines.append("")

    for entry in schedule.tasks:
        lines.append("")
        lines.append("-" * 72)
        lines.append(f"Task: {entry.task.name}" + ("" if entry.task.enabled else "   (disabled)"))
        lines.append("-" * 72)
        if entry.task.purpose:
            lines.append(entry.task.purpose)
            lines.append("")
        lines.append(f"  Cadence   {entry.task.cadence}")
        lines.append(f"  Day       {entry.day or '-'}")
        lines.append(f"  Time      {entry.at or 'NOT SET'} ({schedule.timezone})")
        lines.append(f"  Model     {entry.task.model}")
        lines.append(f"  Mode      {entry.task.mode}")
        lines.append(f"  Tools     {', '.join(entry.task.tools) or '-'}")
        lines.append(f"  Next run  {entry.next_run or '-'}")
        for note in entry.notes:
            lines.append(f"  Note      {note}")
        lines.append("")
        lines.append("  Prompt (paste this whole block):")
        lines.append("")
        for line in entry.task.prompt.strip().splitlines():
            lines.append(f"    {line}")
        lines.append("")
    return "\n".join(lines)


def schedule_payload(
    conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """The ``cowork_schedule`` tool's payload, or the reason there is not one.

    Never raises. A broken task file must not take down the MCP endpoint that a
    lineup run depends on; the session is told what is wrong and carries on with
    the actions it was given.
    """
    try:
        return as_json(render(conn, settings, now=now))
    except CoworkConfigError as exc:
        return {
            "timezone": settings.cowork.timezone,
            "tasks": [],
            "warnings": [f"hal-mary cannot read its own Cowork task file: {exc}"],
            "waivers": {"known": False},
        }
