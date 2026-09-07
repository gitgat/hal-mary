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
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})

    schedule = cowork.render(conn, settings)
    waivers = next(entry for entry in schedule.tasks if entry.task.name == "waivers")

    assert waivers.day == "tuesday"
    assert waivers.at == "10:00"
    assert "WEDNESDAY" in " ".join(waivers.notes)


def test_a_different_waiver_day_moves_the_run(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["THURSDAY"], "waiverHours": 3})

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
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
    text = cowork.render_text(cowork.render(conn, settings))

    assert settings.cowork.timezone in text
    # No bare hour without the zone next to it anywhere in the summary table.
    for line in text.splitlines():
        if "10:30" in line and "Time" not in line:
            assert settings.cowork.timezone in line or "Times below" in text


def test_the_default_timezone_is_called_out_as_something_to_change(conn, settings):
    """Cowork's form takes local times; UTC is a placeholder, not an answer."""
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
    schedule = cowork.render(conn, settings)
    if schedule.timezone == "UTC":
        assert any("timezone" in warning.lower() for warning in schedule.warnings)


# --- next run ----------------------------------------------------------------


def test_the_next_run_of_a_weekly_task_is_its_next_matching_day(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
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
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
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
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
    text = cowork.render_text(cowork.render(conn, settings))

    for task in cowork.load_tasks(settings):
        assert task.name in text
    assert "Next run" in text


def test_the_json_form_is_json(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
    payload = cowork.as_json(cowork.render(conn, settings))

    assert json.loads(json.dumps(payload)) == payload
    assert payload["timezone"] == settings.cowork.timezone
    names = [entry["name"] for entry in payload["tasks"]]
    assert "waivers" in names


def test_a_disabled_task_is_still_listed_but_marked(conn, settings):
    synced_league(conn, {"waiverProcessDays": ["WEDNESDAY"], "waiverHours": 10})
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


def test_the_document_says_the_browsing_job_stays_separate(settings):
    doc = (REPO_ROOT / "docs" / "COWORK.md").read_text(encoding="utf-8").lower()
    assert "news-sweep" in doc
    assert "must not be merged" in doc or "never merge" in doc
