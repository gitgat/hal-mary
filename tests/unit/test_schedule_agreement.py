"""The two halves of hal-mary's week have to agree with each other.

There are two schedules. hal-mary's own jobs run in-process on APScheduler, in
``[scheduler].timezone``. Claude Cowork's jobs run in Cowork, at times a person
pastes into its form from ``hal-mary cowork-config``, in ``[cowork].timezone``.

They are not independent. hal-mary works out what should change and queues it;
Cowork opens ESPN and performs it. **So every Cowork lineup run has to happen
after the hal-mary lineup check that fills its queue** — and if the two zones
drift apart, that ordering inverts silently: Cowork finds an empty queue, says
"nothing to do", and is right, and the lineup never changes.

Two settings that must agree and are never compared is a bug waiting for
whoever edits one of them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from draft_fixtures import make_settings, open_db

from hal_mary import cowork
from hal_mary.jobs.scheduler import scheduler_timezone

#: Cowork lineup task -> the weekday its hal-mary counterpart must precede it on.
LINEUP_PAIRS = {"lineup-sunday": "Sun", "lineup-thursday": "Thu", "lineup-monday": "Mon"}

#: When the real games start, local. The Sunday early window kicks at 10:00
#: Pacific (13:00 Eastern); the Thursday and Monday night games at 17:15.
KICKOFF_HOUR = {"Sun": 10, "Thu": 17, "Mon": 17}

FROM = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # a Monday


def test_the_two_timezones_agree(tmp_path):
    """One box, one household, one set of kickoffs. They may be separate keys —
    the box's zone and the operator's are different ideas — but a shipped config
    in which they disagree is a schedule whose two halves are hours apart."""
    settings = make_settings(tmp_path)
    assert settings.cowork.timezone == settings.scheduler.timezone


def test_neither_schedule_is_left_in_utc(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.cowork.timezone != "UTC"
    assert settings.scheduler.timezone != "UTC"


def local_fire_hours(settings, day: str) -> list[float]:
    """The hours, local, at which hal-mary's lineup check fires on ``day``."""
    from apscheduler.triggers.cron import CronTrigger

    zone = scheduler_timezone(settings)
    hours = []
    for cron in settings.job("lineup_check").crons:
        moment = CronTrigger.from_crontab(cron, timezone=zone).get_next_fire_time(None, FROM)
        local = moment.astimezone(zone)
        if local.strftime("%a") == day:
            hours.append(local.hour + local.minute / 60)
    return hours


def cowork_hours(tmp_path, name: str) -> float:
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    schedule = cowork.render(conn, settings, now=FROM)
    entry = next(item for item in schedule.tasks if item.task.name == name)
    hour, minute = (int(part) for part in entry.at.split(":"))
    return hour + minute / 60


def test_every_cowork_lineup_run_happens_after_the_check_that_fills_its_queue(tmp_path):
    """hal-mary decides, Cowork performs. The other order performs nothing."""
    settings = make_settings(tmp_path)
    for name, day in LINEUP_PAIRS.items():
        checks = local_fire_hours(settings, day)
        assert checks, f"hal-mary has no lineup check on {day}, so {name} has nothing to perform"
        assert cowork_hours(tmp_path, name) > max(checks), (
            f"{name} runs at {cowork_hours(tmp_path, name)}, before hal-mary's "
            f"{day} check at {max(checks)} — it would find an empty queue"
        )


def test_both_schedules_finish_before_the_ball_is_kicked(tmp_path):
    """A lineup change after kickoff is worth nothing: ESPN locks each player at
    his own game, so the window closes one player at a time."""
    settings = make_settings(tmp_path)
    for name, day in LINEUP_PAIRS.items():
        kickoff = KICKOFF_HOUR[day]
        for hour in local_fire_hours(settings, day):
            assert hour < kickoff, f"hal-mary's {day} check at {hour} is after kickoff"
        assert cowork_hours(tmp_path, name) < kickoff, f"{name} runs after kickoff"


def test_the_rendered_schedule_says_so_when_the_two_zones_disagree(tmp_path):
    """Loud where the operator is actually looking — in the output they are
    about to paste — not in a log line nobody reads."""
    # Anchored on the line above it: both sections now name the same zone, so
    # the bare assignment is no longer unique in the file.
    settings = make_settings(
        tmp_path,
        replace={
            'a test refuses a config in which they do.\ntimezone = "America/Los_Angeles"':
            'a test refuses a config in which they do.\ntimezone = "America/New_York"'
        },
    )
    conn = open_db(tmp_path)

    schedule = cowork.render(conn, settings, now=FROM)

    assert any("America/New_York" in warning for warning in schedule.warnings), schedule.warnings
    assert any(settings.scheduler.timezone in warning for warning in schedule.warnings)


def test_agreeing_zones_produce_no_such_warning(tmp_path):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    schedule = cowork.render(conn, settings, now=FROM)
    assert not [w for w in schedule.warnings if "scheduler].timezone" in w]


def test_the_summary_table_still_lines_up_with_a_real_timezone_name(tmp_path):
    """"America/Los_Angeles" is nineteen characters. The When column was sized
    for "UTC", so a real zone pushed every following column out of true — in the
    one output whose whole job is being read by a person."""
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)

    text = cowork.render_text(cowork.render(conn, settings, now=FROM))
    rows = [line for line in text.splitlines() if " enabled " in line or " off " in line]

    assert rows
    offsets = {line.index("execute" if "execute" in line else "read_only") for line in rows}
    assert len(offsets) == 1, f"the Mode column starts at {sorted(offsets)}"
