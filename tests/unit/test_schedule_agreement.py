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

from draft_fixtures import make_settings, open_db, seed_synced_league

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


def local_fire_hours(settings, day: str, job: str = "lineup_check") -> list[float]:
    """The hours, local, at which ``job`` fires on ``day``.

    Defaults to the lineup check because that is what the pairs above are
    about; the feed-ordering test below passes its own job name.
    """
    from apscheduler.triggers.cron import CronTrigger

    zone = scheduler_timezone(settings)
    hours = []
    for cron in settings.job(job).crons:
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


#: A Cowork task that *feeds* a hal-mary job, and the job it must run before.
#: The lineup pairs above are the other direction — hal-mary decides, Cowork
#: performs. This one is Cowork observing and hal-mary reasoning afterwards, and
#: it inverts just as silently.
FEEDS_INTO = {"postweek-observations": "waiver_scan"}


def test_a_cowork_task_that_feeds_a_job_runs_before_it(tmp_path):
    """Observations are worthless to a decision already made.

    `postweek-observations` reads what Monday night actually did to her players
    and files notes. `waiver_scan` reads those notes to decide which claims go
    in the queue. Scheduled after the scan, the notes miss the decision they are
    most relevant to and sit in SQLite until the *following* Tuesday — a week
    late, every week, and nothing anywhere reports a problem.

    Same shape as the lineup inversion above, displaced by an hour instead of a
    day, and just as quiet: the run succeeds, the notes are written, and the
    claims were simply decided without them.
    """
    settings = make_settings(tmp_path)

    for task_name, job_name in FEEDS_INTO.items():
        cowork_at = cowork_hours(tmp_path, task_name)
        job_at = min(local_fire_hours(settings, "Tue", job=job_name))
        assert cowork_at < job_at, (
            f"{task_name} runs at {cowork_at} but {job_name} reads its notes at "
            f"{job_at}; the observations would be a week late to the decision"
        )


# --- the phase check has to keep up with the windows it evaluates ------------


def test_the_phase_check_catches_up_before_the_first_in_season_job(tmp_path):
    """The defect: an evening draft leaves the phase stale for a whole extra day.

    ``current_phase`` is exact — it compares the clock to the draft window every
    time it is asked. But it is only *asked* on ``[scheduler].phase_cron``, and
    nothing schedules a job between one asking and the next. So the resolution
    of the whole phase machine is the cadence of that cron, not the precision of
    the function.

    A fantasy league drafts in the evening. With a 12-hour after-window that
    closes mid-morning, a once-daily check that runs at 04:20 asks the question
    *before* the window shuts, gets "still drafting", and does not ask again for
    24 hours. ``draft_live`` schedules no jobs at all, so every in-season job due
    in between is not merely late — it is never registered, and its fire is lost
    in silence. On the real draft this ate week one's Wednesday news sweep, and
    the Thursday lineup decision would have been made with no in-season research
    behind it at all.

    This is the same family as the two ordering bugs already recorded here: a
    thing that must happen before another thing, with nothing comparing them.
    """
    from datetime import timedelta

    from apscheduler.triggers.cron import CronTrigger

    from hal_mary.jobs.registry import specs_for_phase
    from hal_mary.jobs.scheduler import current_phase

    settings = make_settings(tmp_path)
    zone = scheduler_timezone(settings)
    conn = open_db(tmp_path)

    # 18:00 Pacific on a Tuesday — when this league actually drafted.
    draft_at = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
    seed_synced_league(conn, draft_date=draft_at.isoformat())
    assert current_phase(conn, settings, now=draft_at) == "draft_live"

    closes = draft_at + timedelta(hours=settings.scheduler.draft_window_after_hours)

    # When the phase check next runs *and* sees that the draft is behind it.
    trigger = CronTrigger.from_crontab(settings.scheduler.phase_cron, timezone=zone)
    previous, cursor, flips_at = None, closes, None
    for _ in range(200):
        moment = trigger.get_next_fire_time(previous, cursor)
        if current_phase(conn, settings, now=moment) == "in_season":
            flips_at = moment
            break
        previous, cursor = moment, moment
    assert flips_at is not None, "the phase never becomes in_season"

    # The first in-season job due after the window shuts. It is only scheduled
    # if the phase has already flipped, so this fire is the deadline.
    earliest, whose = None, ""
    for spec in specs_for_phase("in_season"):
        for cron in settings.job(spec.name).crons:
            fire = CronTrigger.from_crontab(cron, timezone=zone).get_next_fire_time(None, closes)
            if earliest is None or fire < earliest:
                earliest, whose = fire, spec.name

    assert earliest is not None, "no in-season job has a cadence"
    assert flips_at <= earliest, (
        f"the draft window shuts at {closes}, but the phase is not re-checked "
        f"until {flips_at} — so {whose} at {earliest} comes due while hal-mary "
        f"still believes it is drafting, and is never scheduled at all"
    )
