"""Tests for the slow half of draft night: the pre-draft board build.

This job is allowed to take fifteen minutes because it runs the day before. Two
properties matter more than the mapping:

* **A successful build replaces the board atomically and leaves notes behind**,
  keyed by the exact name it wrote on the board — Task 4 normalises both sides of
  a name comparison but cannot repair a name that was never written.
* **A failed build leaves the previous board completely untouched.** A stale
  board beats no board on draft night, and an empty board means the advisor has
  nothing to recommend from at all.
"""

from __future__ import annotations

import json

from draft_fixtures import (
    REPO,
    SAMPLE_BOARD,
    FakeRunner,
    failed_result,
    make_settings,
    ok_result,
    open_db,
    seed_board,
    seed_synced_league,
)

from hal_mary.jobs.board_build import build_board
from hal_mary.memory import search_notes

PLAYERS = [
    {
        "name": "Ja'Marr Chase",
        "position": "WR",
        "pro_team": "CIN",
        "tier": 1,
        "rank": 1,
        "bye_week": 10,
        "note": "He catches more passes than almost anyone, and every catch is a point here.",
        "source_url": "https://example.com/rankings",
    },
    {
        "name": "Bijan Robinson",
        "position": "RB",
        "pro_team": "ATL",
        "tier": 1,
        "rank": 2,
        "bye_week": 5,
        "note": "He runs the ball and catches it, so he is on the field on every down.",
        "source_url": "https://example.com/adp",
    },
    {
        "name": "Brock Bowers",
        "position": "TE",
        "pro_team": "LV",
        "tier": 2,
        "rank": 3,
        "bye_week": 8,
        "note": "",
    },
]


def build_ready(tmp_path, results):
    """A migrated database with the real league synced, and a fake runner."""
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    return conn, settings, FakeRunner(settings, results)


def test_a_successful_build_replaces_the_board(tmp_path):
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])
    seed_board(conn, [{"name": "Somebody Stale", "position": "RB", "tier": 1, "rank": 1}])

    result = build_board(conn, settings, runner)

    assert result["ok"] is True
    assert result["players"] == 3
    rows = conn.execute("SELECT name, position, tier, rank FROM board ORDER BY rank").fetchall()
    assert [row["name"] for row in rows] == [
        "Ja'Marr Chase",
        "Bijan Robinson",
        "Brock Bowers",
    ]
    assert "Somebody Stale" not in [row["name"] for row in rows]


def test_a_successful_build_writes_a_note_per_player_that_said_something(tmp_path):
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    result = build_board(conn, settings, runner)

    assert result["notes"] == 2, "the empty note is not worth a row"
    notes = conn.execute("SELECT player_name, text, source_url FROM notes").fetchall()
    assert {note["player_name"] for note in notes} == {"Ja'Marr Chase", "Bijan Robinson"}
    assert all(note["source_url"] for note in notes), "a researched claim carries its source"


def test_notes_use_the_name_exactly_as_it_was_written_to_the_board(tmp_path):
    """The advisor retrieves by name later. A note filed under a name the board
    does not use is a note that will never be found again."""
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    build_board(conn, settings, runner)

    board_names = {row["name"] for row in conn.execute("SELECT name FROM board")}
    note_names = {
        row["player_name"]
        for row in conn.execute("SELECT player_name FROM notes WHERE player_name IS NOT NULL")
    }
    assert note_names <= board_names
    assert search_notes(conn, "Chase")


def test_a_failed_call_leaves_the_previous_board_intact_and_records_the_error(tmp_path):
    """A stale board beats no board while a 90-second clock is running."""
    conn, settings, runner = build_ready(
        tmp_path, [failed_result(text="I was still searching when time ran out", error="timeout")]
    )
    seed_board(conn, SAMPLE_BOARD)
    before = conn.execute("SELECT name FROM board ORDER BY rank").fetchall()

    result = build_board(conn, settings, runner)

    assert result["ok"] is False
    after = conn.execute("SELECT name FROM board ORDER BY rank").fetchall()
    assert [row["name"] for row in after] == [row["name"] for row in before]
    run = conn.execute("SELECT status, error FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "error"
    assert "timeout" in run["error"]


def test_an_empty_player_list_is_a_failure_not_an_empty_board(tmp_path):
    """A board of nobody is indistinguishable from a board that never built, and
    the advisor would have nothing to recommend from."""
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": []})])
    seed_board(conn, SAMPLE_BOARD)

    result = build_board(conn, settings, runner)

    assert result["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM board").fetchone()[0] == len(SAMPLE_BOARD)


def test_a_run_that_raises_is_recorded_rather_than_escaping(tmp_path):
    conn, settings, runner = build_ready(tmp_path, [RuntimeError("the binary is missing")])
    seed_board(conn, SAMPLE_BOARD)

    result = build_board(conn, settings, runner)

    assert result["ok"] is False
    assert "binary is missing" in result["error"]
    assert conn.execute("SELECT COUNT(*) FROM board").fetchone()[0] == len(SAMPLE_BOARD)


def test_a_successful_run_is_recorded_as_ok(tmp_path):
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    build_board(conn, settings, runner)

    run = conn.execute("SELECT status, summary FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "ok"
    assert "3" in run["summary"]


def test_the_research_job_runs_with_web_tools_on(tmp_path):
    """The mirror of the advisor's guard. Football facts never come from training
    knowledge, so this call — and only this half of draft night — searches."""
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    build_board(conn, settings, runner)

    assert runner.calls[0]["job"] == "board_build"
    assert settings.job("board_build").tools, "board research needs web tools"


def test_the_prompt_carries_the_real_league_facts(tmp_path):
    """A model given no league context assumes twelve teams and standard scoring,
    and gives confidently wrong advice from the first pick."""
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    build_board(conn, settings, runner)
    prompt = runner.calls[0]["prompt"]

    assert "6 teams" in prompt
    assert "full PPR" in prompt
    assert "6 and 7" in prompt, "her first two picks are back to back and that changes the board"
    assert "16 rounds" in prompt


def test_the_schema_asks_for_the_fields_the_board_table_holds(tmp_path):
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    build_board(conn, settings, runner)
    schema = runner.calls[0]["schema"]

    item = schema["properties"]["players"]["items"]["properties"]
    assert set(item) >= {"name", "position", "pro_team", "tier", "rank", "bye_week", "note"}


def test_every_board_row_is_tiered(tmp_path):
    """`scarcity` reports a best_tier of None for a position with no tiered rows,
    and a count that then means nothing. The defence is here: no untiered row
    ever reaches the board."""
    players = [
        {**PLAYERS[0], "tier": None},
        {**PLAYERS[1], "tier": 2},
        {**PLAYERS[2], "tier": None, "rank": 3},
    ]
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": players})])

    build_board(conn, settings, runner)

    tiers = [row["tier"] for row in conn.execute("SELECT tier FROM board ORDER BY rank")]
    assert all(tier is not None for tier in tiers)
    assert tiers == [1, 2, 2], "a missing tier carries forward rather than inventing a gap"


def test_board_rows_reuse_the_espn_player_id_when_the_name_is_known(tmp_path):
    """Matching a pick by id is the only stage that still works when ESPN's name
    map is briefly unavailable, so use a real id wherever one exists."""
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])
    conn.execute(
        "INSERT INTO players (player_id, name, position) VALUES (4262921, 'Ja''Marr Chase', 'WR')"
    )
    conn.commit()

    build_board(conn, settings, runner)

    ids = dict(conn.execute("SELECT name, player_id FROM board"))
    assert ids["Ja'Marr Chase"] == 4262921
    # ESPN pre-populates unmade picks with playerId -1, so a synthetic id must
    # never land near it or an unmade pick would match a real board row.
    assert all(value < -1 for name, value in ids.items() if name != "Ja'Marr Chase")


def test_the_prompt_file_is_a_real_deliverable():
    """It is the only place the football strategy for this league is written down."""
    text = (REPO / "prompts" / "board_build.md").read_text(encoding="utf-8").lower()

    assert len(text) > 1500
    for required in (
        "search",
        "average draft position",
        "tier",
        "injur",
        "rookie",
        "cite",
        "bye week",
    ):
        assert required in text, f"prompts/board_build.md never mentions {required!r}"


def test_the_prompt_file_states_no_league_fact_of_its_own():
    """Every league fact reaches the prompt through a placeholder.

    Writing "six-team" or "full PPR" into the file beside the placeholder that
    carries the same fact means the two can disagree — and they will, the first
    time the `[league]` config fallback describes a different league. A prompt
    that contradicts itself is worse than one that is merely vague, because the
    model has to guess which half to believe.
    """
    text = (REPO / "prompts" / "board_build.md").read_text(encoding="utf-8").lower()

    for hardcoded in ("six-team", "six team", "full ppr", "half ppr", "drafts last", "snake"):
        assert hardcoded not in text, (
            f"prompts/board_build.md hardcodes {hardcoded!r}; it must come from a placeholder"
        )
    for placeholder in ("{{team_count}}", "{{scoring_summary}}", "{{draft_type}}",
                        "{{my_draft_slot}}", "{{first_two_picks}}"):
        assert placeholder in text.replace("{{scoring_summary}}", "{{scoring_summary}}"), (
            f"prompts/board_build.md never uses {placeholder}"
        )


def test_the_prompt_file_explains_the_beginner_reading_it():
    text = (REPO / "prompts" / "board_build.md").read_text(encoding="utf-8").lower()
    assert "flex" in text and "bye week" in text
    assert "jargon" in text or "beginner" in text or "does not know" in text


def test_memory_is_passed_through_extra_context_not_concatenated(tmp_path):
    conn, settings, runner = build_ready(tmp_path, [ok_result({"players": PLAYERS})])

    build_board(conn, settings, runner)

    assert runner.calls[0]["extra_context"], "standing memory reaches the model"
    assert json.dumps(runner.calls[0]["extra_context"]) not in json.dumps(
        runner.calls[0]["prompt"]
    )
