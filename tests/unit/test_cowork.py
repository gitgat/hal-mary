"""Tests for the Cowork task configuration and the renderer that prints it.

Cowork's scheduled tasks each carry a saved prompt, and that prompt *is* the cron
job. So the prompts are a shipped artifact rather than documentation, and they
live in ``cowork/tasks.toml`` as data: adding a job must not require code.

Two properties earn most of the tests.

**A read-only job cannot act.** ``mode = "read_only"`` means the session that
reads other people's page text has no acting tool in its list at all. That is
structural, not a promise in prose, and the loader refuses a file that breaks it.

**The schedule is this league's, not a generic one.** The waiver run's time comes
from the league's own waiver processing day, because a claim submitted after
processing is worth nothing. When the setting is absent the renderer says so
loudly instead of assuming Wednesday.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from draft_fixtures import make_settings

from conftest import FIXTURE_ENV
from hal_mary import cowork, db
from hal_mary.config import load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(env={**FIXTURE_ENV, "DB_PATH": str(tmp_path / "hal.db")})



DAY_ORDER = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

_CRON_DAYS = {"mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday",
              "fri": "friday", "sat": "saturday", "sun": "sunday"}


def _cron_day_and_hour(cron: str) -> tuple[str, int]:
    """The weekday and hour a one-day crontab string names."""
    fields = cron.split()
    return _CRON_DAYS[fields[4].split(",")[0][:3].lower()], int(fields[1])



def _settings_with_scan(tmp_path: Path, cron: str):
    """Settings whose waiver scan runs at ``cron``, for the ordering property.

    Goes through ``make_settings`` so the temporary config keeps the anchored
    paths pointing back at the repo — a bare copy in ``tmp_path`` has no
    ``cowork/tasks.toml`` beside it and the renderer refuses it.
    """
    return make_settings(tmp_path, replace={'cron = "0 8 * * tue"': f'cron = "{cron}"'})


def synced_league(conn: sqlite3.Connection, acquisition: dict | None) -> None:
    raw = {"acquisitionSettings": acquisition} if acquisition is not None else {}
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO league_settings
                (id, season, league_id, name, team_count, roster_slots_json, raw_json,
                 updated_at, current_week)
            VALUES (1, 2026, 7654321, 'The Invented League', 6, '{}', ?, ?, 5)
            """,
            (json.dumps(raw), "2026-10-01T12:00:00+00:00"),
        )


def write_tasks(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tasks.toml"
    path.write_text(body, encoding="utf-8")
    return path


ONE_TASK = """
[[task]]
name = "lineup-sunday"
purpose = "Perform whatever hal-mary has queued before the early games."
enabled = true
cadence = "weekly"
day = "sunday"
at = "10:30"
model = "opus"
mode = "execute"
tools = ["pending_actions", "report_action", "report_observation"]
prompt = "Call pending_actions and do exactly what it returns."
"""


# --- the shipped file --------------------------------------------------------


def test_the_shipped_task_file_loads(settings):
    tasks = cowork.load_tasks(settings)
    assert tasks, "cowork/tasks.toml should ship with jobs in it"
    assert len({task.name for task in tasks}) == len(tasks)


def test_the_three_jobs_that_matter_are_enabled_and_the_rest_are_not(settings):
    """The file documents the whole menu; only the three that matter run on day one."""
    enabled = {task.name for task in cowork.load_tasks(settings) if task.enabled}
    assert enabled == {"lineup-sunday", "lineup-thursday", "waivers", "news-sweep"}


def test_every_shipped_task_carries_a_prompt_that_says_the_three_things(settings):
    """The empty case, reporting every outcome, and never acting on a surprise."""
    for task in cowork.load_tasks(settings):
        lowered = task.prompt.lower()
        assert "report_observation" in lowered, task.name
        if task.mode == "execute":
            assert "nothing to do" in lowered, task.name
            assert "report_action" in lowered, task.name
            assert "sequence" in lowered, task.name


def test_no_shipped_prompt_names_a_player_a_week_or_a_strategy(settings):
    """The saved prompt is static and generic; hal-mary carries the intelligence.

    A prompt that names anything specific has to be edited every week, and the
    moment Cowork's saved text carries strategy the boundary has moved.
    """
    for task in cowork.load_tasks(settings):
        lowered = task.prompt.lower()
        for forbidden in ("week 1", "week 5", "bijan", "quarterback", "start the best"):
            assert forbidden not in lowered, f"{task.name} names something specific: {forbidden}"


def test_every_tool_a_shipped_task_lists_is_a_tool_the_server_offers(settings):
    for task in cowork.load_tasks(settings):
        for tool in task.tools:
            assert tool in cowork.KNOWN_TOOLS, f"{task.name} lists an unknown tool {tool!r}"


# --- validation --------------------------------------------------------------


def test_a_read_only_task_may_not_list_an_acting_tool(tmp_path, settings):
    """Structural, not a promise. The browsing session cannot act on what it reads."""
    path = write_tasks(
        tmp_path,
        """
[[task]]
name = "news"
purpose = "Look for news."
enabled = true
cadence = "daily"
at = "07:00"
model = "opus"
mode = "read_only"
tools = ["get_roster", "report_action", "report_observation"]
prompt = "Look for news and call report_observation."
""",
    )
    with pytest.raises(cowork.CoworkConfigError) as excinfo:
        cowork.load_tasks(settings, path)
    assert "report_action" in str(excinfo.value)
    assert "news" in str(excinfo.value)


def test_no_shipped_read_only_task_can_act(settings):
    for task in cowork.load_tasks(settings):
        if task.mode == "read_only":
            assert not (set(task.tools) & cowork.ACTING_TOOLS), task.name


def test_an_unknown_cadence_is_refused(tmp_path, settings):
    path = write_tasks(tmp_path, ONE_TASK.replace('cadence = "weekly"', 'cadence = "fortnightly"'))
    with pytest.raises(cowork.CoworkConfigError) as excinfo:
        cowork.load_tasks(settings, path)
    assert "fortnightly" in str(excinfo.value)


def test_a_weekly_task_without_a_day_is_refused(tmp_path, settings):
    path = write_tasks(tmp_path, ONE_TASK.replace('day = "sunday"\n', ""))
    with pytest.raises(cowork.CoworkConfigError):
        cowork.load_tasks(settings, path)


def test_a_task_with_no_prompt_is_refused(tmp_path, settings):
    path = write_tasks(tmp_path, ONE_TASK.replace('prompt = "Call pending_actions and do exactly what it returns."', 'prompt = "  "'))
    with pytest.raises(cowork.CoworkConfigError):
        cowork.load_tasks(settings, path)


def test_an_unknown_tool_is_refused(tmp_path, settings):
    path = write_tasks(tmp_path, ONE_TASK.replace('"report_action"', '"drop_everyone"'))
    with pytest.raises(cowork.CoworkConfigError) as excinfo:
        cowork.load_tasks(settings, path)
    assert "drop_everyone" in str(excinfo.value)


def test_two_tasks_with_the_same_name_are_refused(tmp_path, settings):
    path = write_tasks(tmp_path, ONE_TASK + ONE_TASK)
    with pytest.raises(cowork.CoworkConfigError):
        cowork.load_tasks(settings, path)


def test_a_missing_file_is_refused_by_name(tmp_path, settings):
    with pytest.raises(cowork.CoworkConfigError) as excinfo:
        cowork.load_tasks(settings, tmp_path / "nope.toml")
    assert "nope.toml" in str(excinfo.value)


# --- the waiver derivation ---------------------------------------------------


def test_the_waiver_run_is_timed_from_the_leagues_own_processing_day(conn, settings):
    """A claim submitted after processing is worth nothing."""
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")

    assert waivers.day == "tuesday"
    assert waivers.at == "10:00"
    assert "wednesday" in " ".join(waivers.notes).lower()


def test_a_different_waiver_day_moves_the_run(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["THURSDAY"], "waiverProcessHour": 3})

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")
    assert waivers.day == "wednesday"
    assert waivers.at == "03:00"


def test_an_absent_waiver_setting_is_said_loudly_rather_than_assumed(conn, settings):
    synced_league(conn, None)

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")

    assert waivers.at is None
    assert waivers.day is None
    joined = " ".join([*schedule.warnings, *waivers.notes]).lower()
    assert "waiver" in joined
    assert "wednesday" not in joined, "the renderer must not quietly assume ESPN's default"


def test_an_unsynced_league_warns_rather_than_printing_a_confident_schedule(conn, settings):
    schedule = cowork.render(conn, settings)
    assert any("sync" in warning.lower() for warning in schedule.warnings)


# --- timezone ----------------------------------------------------------------


def test_every_time_is_printed_beside_its_timezone(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})
    text = cowork.render_text(cowork.render(conn, settings))

    assert settings.cowork.timezone in text
    # No bare hour without the zone next to it anywhere in the summary table.
    # Matched by shape rather than by a literal time: this used to look for
    # "10:30", and when the shipped times moved the loop stopped finding any
    # line at all and checked nothing.
    clock = re.compile(r"\b\d{1,2}:\d{2}\b")
    checked = 0
    for line in text.splitlines():
        if clock.search(line) and "Time" not in line:
            checked += 1
            assert settings.cowork.timezone in line or "Times below" in text
    assert checked, f"no line in the summary carried a time at all:\n{text}"


def test_the_default_timezone_is_called_out_as_something_to_change(conn, tmp_path):
    """Cowork's form takes local times; UTC is a placeholder, not an answer.

    This builds a UTC config rather than reading the shipped one. It used to
    guard the assertion with ``if schedule.timezone == "UTC"``, which was true
    while the shipped default was UTC and silently stopped being true the day
    that default changed — leaving a test that ran no assertions at all and
    still passed, which is the failure mode this repo keeps finding.
    """
    # Both zones, so the "these two disagree" warning cannot fire and stand in
    # for the one under test. With only [cowork] flipped, the drift warning also
    # contains the word "timezone" and this test passes with the UTC warning
    # deleted outright — which is exactly what it did on the first attempt.
    # Two entries because write_config replaces one occurrence per key, and the
    # file carries the same line under [scheduler] and again under [cowork]. The
    # anchored one goes first so it cannot be shadowed by the bare one.
    settings = make_settings(
        tmp_path,
        replace={
            "# when they differ and a test refuses a config in which they do.\n"
            'timezone = "America/Los_Angeles"': (
                "# when they differ and a test refuses a config in which they do.\n"
                'timezone = "UTC"'
            ),
            'timezone = "America/Los_Angeles"': 'timezone = "UTC"',
        },
    )
    assert settings.cowork.timezone == "UTC", "the UTC config did not take effect"
    assert settings.scheduler.timezone == "UTC"
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})

    schedule = cowork.render(conn, settings)

    assert schedule.timezone == "UTC"
    utc_warnings = [w for w in schedule.warnings if "utc" in w.lower()]
    assert utc_warnings, f"nothing warned that UTC is a placeholder: {schedule.warnings}"



# --- next run ----------------------------------------------------------------


def test_the_next_run_of_a_weekly_task_is_its_next_matching_day(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})
    # A Friday.
    now = datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("UTC"))

    schedule = cowork.render(conn, settings, now=now)
    sunday = next(entry for entry in schedule.tasks if entry.task.name == "lineup-sunday")
    assert sunday.next_run.startswith("2026-10-04")


def test_a_manual_task_has_no_next_run(conn, settings, tmp_path):
    path = write_tasks(
        tmp_path,
        ONE_TASK.replace('cadence = "weekly"', 'cadence = "manual"').replace(
            'day = "sunday"\n', ""
        ),
    )
    tasks = cowork.load_tasks(settings, path)
    schedule = cowork.render(conn, settings, tasks=tasks)
    assert schedule.tasks[0].next_run is None


# --- the rendered forms ------------------------------------------------------


def test_the_human_form_carries_every_field_the_cowork_form_asks_for(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})
    text = cowork.render_text(cowork.render(conn, settings))

    assert "lineup-sunday" in text
    assert "Cadence" in text
    assert "Model" in text
    assert "Prompt" in text
    # The whole prompt, ready to paste, not a summary of it.
    prompt = next(task for task in cowork.load_tasks(settings) if task.name == "lineup-sunday")
    for line in prompt.prompt.strip().splitlines():
        assert line.strip() in text


def test_the_summary_table_shows_the_whole_week_at_a_glance(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})
    text = cowork.render_text(cowork.render(conn, settings))

    for task in cowork.load_tasks(settings):
        assert task.name in text
    assert "Next run" in text


def test_the_json_form_is_json(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})
    payload = cowork.as_json(cowork.render(conn, settings))

    assert json.loads(json.dumps(payload)) == payload
    assert payload["timezone"] == settings.cowork.timezone
    names = [entry["name"] for entry in payload["tasks"]]
    assert "waivers" in names


def test_a_disabled_task_is_still_listed_but_marked(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 10})
    payload = cowork.as_json(cowork.render(conn, settings))

    by_name = {entry["name"]: entry for entry in payload["tasks"]}
    assert any(entry["enabled"] is False for entry in by_name.values())


# --- the document ------------------------------------------------------------


def test_every_enabled_prompt_appears_verbatim_in_the_document(settings):
    """The doc and the config cannot drift, because this test would fail first."""
    doc = (REPO_ROOT / "docs" / "COWORK.md").read_text(encoding="utf-8")
    for task in cowork.load_tasks(settings):
        if not task.enabled:
            continue
        for line in task.prompt.strip().splitlines():
            if line.strip():
                assert line.strip() in doc, f"{task.name}: {line.strip()!r} is not in COWORK.md"


def _renderer():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "render_cowork_doc", REPO_ROOT / "scripts" / "render_cowork_doc.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_document_is_regenerated_from_the_task_file(settings):
    """Stronger than containment: re-rendering an up-to-date doc changes nothing.

    The prompts are the shipped artifact and they exist in two places. This is
    the check that makes the second copy generated rather than remembered.
    """
    module = _renderer()

    assert module.main(["--check"]) == 0, (
        "docs/COWORK.md is out of date with cowork/tasks.toml; "
        "run `uv run python scripts/render_cowork_doc.py`"
    )


def test_the_document_says_the_browsing_job_stays_separate(settings):
    doc = (REPO_ROOT / "docs" / "COWORK.md").read_text(encoding="utf-8").lower()
    assert "news-sweep" in doc
    assert "must not be merged" in doc or "never merge" in doc


def test_pending_actions_is_an_acting_tool(settings):
    """A read-only job that can fetch the plan but not report it is a trap.

    It would receive the list of changes, have no way to say what it did with
    them, and hal-mary would re-issue every one on the next run. Fetching the
    instruction list is part of acting, so `read_only` may not have it either.
    """
    assert "pending_actions" in cowork.ACTING_TOOLS
    assert "report_action" in cowork.ACTING_TOOLS


def test_a_read_only_task_may_not_fetch_the_action_list(tmp_path, settings):
    path = write_tasks(
        tmp_path,
        """
[[task]]
name = "news"
purpose = "Look for news."
enabled = true
cadence = "daily"
at = "07:00"
model = "opus"
mode = "read_only"
tools = ["get_roster", "pending_actions", "report_observation"]
prompt = "Look for news and call report_observation."
""",
    )
    with pytest.raises(cowork.CoworkConfigError) as excinfo:
        cowork.load_tasks(settings, path)
    assert "pending_actions" in str(excinfo.value)


def test_no_read_only_prompt_claims_cowork_itself_cannot_act(settings):
    """It has a browser and is logged into ESPN. Claim what is true.

    This test used to *require* the phrase "hal-mary has given ... only tools
    that read", on the theory that describing what hal-mary handed over was
    safely true even though describing Cowork as incapable was not. It is not
    safely true: the MCP endpoint gives every tool to anyone with `MCP_TOKEN`,
    with no per-task gating, so a read-only session is holding the acting tools
    too and can prove it with one `list_tools`. The old assertion therefore
    pinned the prompt to a claim the model can disprove.

    What survives is the half that was always right — never say it cannot act —
    plus the instruction that replaces the claim.
    `tests/unit/test_readonly_prompt_truth.py` holds the fuller version.
    """
    for task in cowork.load_tasks(settings):
        if task.mode != "read_only":
            continue
        lowered = task.prompt.lower()
        assert "you have no tool that could" not in lowered, task.name
        assert "there is no tool here" not in lowered, task.name
        assert "do not change anything" in lowered, task.name


def test_a_malformed_waiver_day_is_as_loud_as_an_absent_one(conn, settings):
    """The absent case refuses to assume Wednesday. So must the unreadable one.

    Falling back to an index default reinstated the exact guess the design
    forbade, and did it silently — which is worse than the absence it was
    covering for, because nothing on the page says a guess was made.
    """
    synced_league(conn, {"waiverProcessDays": ["EVERY_OTHER_TUESDAY"], "waiverProcessHour": 10})

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")

    assert waivers.at is None
    assert waivers.day is None
    joined = " ".join([*schedule.warnings, *waivers.notes]).lower()
    assert "wednesday" not in joined
    assert "every_other_tuesday" in joined


def test_a_malformed_waiver_hour_is_refused_too(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 99})

    waivers = cowork.waiver_settings(conn)
    assert waivers["known"] is False
    assert "99" in waivers["reason"]


def test_the_task_file_path_comes_from_settings_already_anchored(settings):
    """`Path(a_path)` is the bug CLAUDE.md names, not a safety net."""
    assert cowork.tasks_path(settings) is settings.paths.cowork_tasks


def test_the_renderer_requires_a_block_for_every_task(settings, tmp_path):
    """A task with no block in the document is drift the check would not see.

    `--check` only compared the blocks that existed, so three shipped jobs had no
    entry in COWORK.md at all and it passed. "Every prompt is in the document" is
    the property; "every block that is there is current" is not.
    """
    module = _renderer()
    doc = (REPO_ROOT / "docs" / "COWORK.md").read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        module.render(doc, {**module.prompts(), "a-task-nobody-documented": "Do a thing."})
    assert "a-task-nobody-documented" in str(excinfo.value)


def test_every_shipped_task_has_a_block_in_the_document(settings):
    doc = (REPO_ROOT / "docs" / "COWORK.md").read_text(encoding="utf-8")
    for task in cowork.load_tasks(settings):
        assert f"<!-- prompt:{task.name} -->" in doc, f"{task.name} has no block in COWORK.md"


# --- the real league's waiver payload ----------------------------------------
#
# Every test above this line hand-wrote ``waiverHours`` as the processing hour.
# The real payload, read off Caroline's league on 2026-09-08, does not agree:
#
#   "waiverHours": 24,            <- how long a player sits on waivers
#   "waiverProcessHour": 11,      <- the hour claims are actually processed
#   "waiverProcessDays": ["MONDAY", "WEDNESDAY", "THURSDAY",
#                         "FRIDAY", "SATURDAY", "SUNDAY"]
#
# So the two fields mean different things, and this league processes on six days
# rather than one. A fixture that says ``waiverHours: 10`` cannot contradict the
# belief that put it there, which is why these tests quote the live payload.

REAL_ACQUISITION = {
    "acquisitionType": "WAIVERS_TRADITIONAL",
    "waiverHours": 24,
    "waiverProcessHour": 11,
    "waiverProcessDays": [
        "MONDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
        "SUNDAY",
    ],
}


def test_the_processing_hour_comes_from_the_field_that_holds_it(conn, settings):
    """``waiverHours`` is the waiver period, not the hour of the day.

    Reading it as the hour makes this league's 24 an impossible clock time, so
    the derivation refuses it and the waiver run never gets a time at all.
    """
    synced_league(conn, REAL_ACQUISITION)

    waivers = cowork.waiver_settings(conn)

    assert waivers["known"], waivers.get("reason")
    assert waivers["process_hour"] == 11


def test_a_league_that_processes_every_day_says_so(conn, settings):
    """Six processing days is not one. Modelling only the first hides five."""
    synced_league(conn, REAL_ACQUISITION)

    waivers = cowork.waiver_settings(conn)

    assert [day.lower() for day in waivers["process_days"]] == [
        "monday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]


@pytest.mark.parametrize("scan_day", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
@pytest.mark.parametrize("scan_hour", [0, 8, 12, 23])
@pytest.mark.parametrize(
    "process_days",
    [
        ["WEDNESDAY"],
        ["MONDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
        ["TUESDAY"],
        ["MONDAY", "THURSDAY"],
    ],
)
def test_the_claim_run_always_lands_between_the_scan_and_the_batch(
    conn, tmp_path, scan_day, scan_hour, process_days
):
    """The pipeline invariant, for waivers, wherever the scan is put.

    hal-mary's waiver scan queues the claims; Cowork submits them. A submit run
    before the scan finds an empty queue, reports nothing to do, and is
    *correct* — so the failure is silent and costs a week of claims. A submit
    run after processing is worth nothing either.

    This is a property, not an example: the derivation is supposed to hold it
    for every scan time, so an example test of one scan time would pass by
    construction and guard nothing.
    """
    settings = _settings_with_scan(tmp_path, f"0 {scan_hour} * * {scan_day}")
    synced_league(conn, {**REAL_ACQUISITION, "waiverProcessDays": process_days})

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")
    assert waivers.day is not None and waivers.at is not None, waivers.notes

    scan = DAY_ORDER.index(_CRON_DAYS[scan_day]) * 1440 + scan_hour * 60
    submit = DAY_ORDER.index(waivers.day) * 1440 + int(waivers.at.split(":")[0]) * 60
    batch = DAY_ORDER.index(waivers.targets_processing_on) * 1440 + REAL_ACQUISITION[
        "waiverProcessHour"
    ] * 60
    # Measured forward from the scan, so the week's wrap-around is not a special
    # case: the submit must come first, then the batch it is aiming at.
    week = 7 * 24 * 60
    assert 0 < (submit - scan) % week < (batch - scan) % week or (batch - scan) % week == 0, (
        f"scan {scan_day} {scan_hour:02d}:00 -> submit {waivers.day} {waivers.at} "
        f"-> batch {waivers.targets_processing_on} 11:00 is not in order"
    )

    # And it must be the *next* batch after the scan, not merely some later one.
    # "Between the scan and the batch" is satisfied by aiming a week out, which
    # keeps the ordering honest while losing every claim to whoever submitted
    # for the batch that came first.
    soonest = min(
        (DAY_ORDER.index(day.lower()) * 1440 + REAL_ACQUISITION["waiverProcessHour"] * 60 - scan)
        % week
        or week
        for day in process_days
    )
    assert (batch - scan) % week == soonest, (
        f"scan {scan_day} {scan_hour:02d}:00 aims at "
        f"{waivers.targets_processing_on}, {(batch - scan) % week // 60}h out, but a batch "
        f"runs {soonest // 60}h out — a claim submitted for the later one arrives after "
        "the earlier one has already been processed"
    )


def test_the_claim_run_still_lands_before_processing(conn, settings):
    """And before the batch it is aiming at, or it submits into a closed window."""
    synced_league(conn, REAL_ACQUISITION)

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")

    target = waivers.targets_processing_on
    assert target is not None
    submit = (DAY_ORDER.index(waivers.day), int(waivers.at.split(":")[0]))
    assert submit < (DAY_ORDER.index(target.lower()), 11), (
        f"the claim run is {waivers.day} {waivers.at}, not before {target} 11:00"
    )
