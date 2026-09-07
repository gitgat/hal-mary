"""Tests for the on-the-clock advisor — the code that has 90 seconds.

The properties here are not about the wording of the advice. They are about the
three ways this can fail while Caroline is staring at a running clock:

* **It must not be slow.** The job is ``draft_advice``, whose configured tool
  list is empty. A web-enabled call on the pick-clock path takes 20 to 120
  seconds against a 90-second clock, so that assertion is a guard on the central
  timing constraint of the whole project, not a style check.
* **It must not come back empty.** A failed call retries once with a shorter
  prompt, and a second failure produces a deterministic recommendation computed
  from the board alone. Caroline never sees an empty card.
* **It must not lose what the model said.** ``ok=False`` still carries the prose
  the model produced; it is logged before falling back, so a bad recommendation
  can be reconstructed afterwards.
"""

from __future__ import annotations

import json
import logging

from draft_fixtures import (
    REPO,
    SAMPLE_BOARD,
    FakeRunner,
    RecordingBus,
    failed_result,
    make_settings,
    ok_result,
    open_db,
    seed_board,
    seed_synced_league,
)

from hal_mary import memory
from hal_mary.draft.advisor import advise
from hal_mary.events import EventBus

ADVICE = {
    "pick": "Saquon Barkley",
    "reason": (
        "He is the best player left and you have no running backs yet. He gets the ball "
        "more than anyone else on his team."
    ),
    "backups": [
        {"name": "Brock Bowers", "reason": "If Barkley goes, take the tight end instead."}
    ],
    "watch_out": "He is on a bye in week 9, the one week his real team does not play.",
}


def advisor_ready(tmp_path, results, board=None, extra=""):
    settings = make_settings(tmp_path, extra)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_board(conn, SAMPLE_BOARD if board is None else board)
    return conn, settings, FakeRunner(settings, results), RecordingBus()


# --- the timing guard --------------------------------------------------------


def test_the_advisor_runs_the_tools_off_job(tmp_path):
    """The guard on the project's central constraint.

    A Claude call with web tools takes 20 to 120 seconds; the pick clock is 90.
    So the on-the-clock call runs against the board that was built yesterday,
    with no tools at all. If this assertion ever fails, draft night is broken.
    """
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    advise(conn, settings, runner, bus, next_overall_pick=6)

    assert runner.calls[0]["job"] == "draft_advice"
    assert settings.job("draft_advice").tools == [], (
        "the on-the-clock job must have no web tools; a searching call cannot "
        "return inside a 90-second pick clock"
    )


# --- the happy path ----------------------------------------------------------


def test_structured_output_is_persisted_and_published(tmp_path):
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert result["pick"] == "Saquon Barkley"
    assert result["source"] == "claude"

    row = conn.execute("SELECT * FROM advice ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "draft"
    assert "Saquon Barkley" in row["headline"]
    assert row["source_job"] == "draft_advice"
    payload = json.loads(row["payload_json"])
    assert payload["backups"][0]["name"] == "Brock Bowers"
    assert payload["next_overall_pick"] == 6

    assert [event for event, _ in bus.published] == ["advice"]
    assert bus.published[0][1]["pick"] == "Saquon Barkley"


async def test_the_advice_event_reaches_a_real_subscriber(tmp_path):
    """The recording bus proves the call; the real bus proves the wiring."""
    conn, settings, runner, _ = advisor_ready(tmp_path, [ok_result(ADVICE)])
    bus = EventBus()

    async with bus.subscribe() as events:
        advise(conn, settings, runner, bus, next_overall_pick=6)
        event, payload = await events.__anext__()

    assert event == "advice"
    assert payload["pick"] == "Saquon Barkley"


def test_one_call_is_enough_when_it_works(tmp_path):
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    advise(conn, settings, runner, bus, next_overall_pick=6)

    assert len(runner.calls) == 1


# --- retry and fallback ------------------------------------------------------


def test_a_failing_call_retries_exactly_once(tmp_path):
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(text="thinking...", error="timeout"), ok_result(ADVICE)]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert len(runner.calls) == 2
    assert result["attempts"] == 2
    assert result["source"] == "claude"
    assert result["pick"] == "Saquon Barkley"


def test_the_retry_is_shorter_and_carries_no_notes(tmp_path):
    """The retry exists because the first attempt was too slow or too big. A
    retry that sends the same prompt again is not a retry."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), ok_result(ADVICE)]
    )
    memory.write_note(
        conn,
        memory.Note(
            text="Saquon Barkley practised in full on Friday.",
            source_job="board_build",
            player_name="Saquon Barkley",
        ),
    )

    advise(conn, settings, runner, bus, next_overall_pick=6)

    first, second = runner.calls
    assert len(second["extra_context"]) < len(first["extra_context"])
    assert "practised in full" in first["extra_context"]
    assert "practised in full" not in second["extra_context"]


def test_unparseable_output_counts_as_a_failure(tmp_path):
    """A call that exits zero and returns prose instead of JSON is exactly as
    useless as one that timed out."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path,
        [ok_result(None, text="I think you should take a running back here."), ok_result(ADVICE)],
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert len(runner.calls) == 2
    assert result["pick"] == "Saquon Barkley"


def test_two_failures_produce_the_deterministic_fallback(tmp_path):
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")]
    )
    # The three best players are gone; the best left at a position she needs is
    # Saquon Barkley.
    conn.execute("UPDATE board SET drafted_by_team_id = 2 WHERE rank <= 3")
    conn.commit()

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert len(runner.calls) == 2, "exactly one retry, then the board decides"
    assert result["source"] == "fallback"
    assert result["pick"] == "Saquon Barkley"
    assert result["reason"], "an empty card is the one thing that must never happen"
    assert conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0] == 1
    assert [event for event, _ in bus.published] == ["advice"]


def test_the_fallback_says_plainly_that_it_is_the_fallback(tmp_path):
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    lowered = result["reason"].lower()
    assert "highest-ranked" in lowered or "top of" in lowered or "best player left" in lowered
    assert "could not" in lowered or "did not" in lowered or "on my own" in lowered


def test_the_fallback_respects_the_positions_she_still_needs(tmp_path):
    """The best player left is not the answer when she cannot start him."""
    board = [
        {"player_id": -1, "name": "Wide Guy", "position": "WR", "tier": 1, "rank": 1},
        {"player_id": -2, "name": "Quarter Back", "position": "QB", "tier": 3, "rank": 9},
        # Everything she has already taken, attributed to her team.
        {"player_id": -3, "name": "Rusher One", "position": "RB", "tier": 1, "rank": 2,
         "drafted_by_team_id": 6},
        {"player_id": -4, "name": "Rusher Two", "position": "RB", "tier": 1, "rank": 3,
         "drafted_by_team_id": 6},
        {"player_id": -5, "name": "Catcher One", "position": "WR", "tier": 1, "rank": 4,
         "drafted_by_team_id": 6},
        {"player_id": -6, "name": "Catcher Two", "position": "WR", "tier": 1, "rank": 5,
         "drafted_by_team_id": 6},
        {"player_id": -7, "name": "Tight One", "position": "TE", "tier": 1, "rank": 6,
         "drafted_by_team_id": 6},
        {"player_id": -8, "name": "Flex Body", "position": "TE", "tier": 1, "rank": 7,
         "drafted_by_team_id": 6},
    ]
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")], board=board
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=30)

    assert result["pick"] == "Quarter Back", "Wide Guy is better, and she cannot start him"


def test_an_empty_board_still_produces_a_card(tmp_path):
    """No board at all is the worst case, and it still must not show nothing."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")], board=[]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert result["source"] == "fallback"
    assert result["reason"]
    assert conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0] == 1


def test_a_runner_that_raises_is_treated_as_a_failed_call(tmp_path):
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [RuntimeError("binary missing"), RuntimeError("binary missing")]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert result["source"] == "fallback"
    assert result["pick"]


def test_the_prose_of_a_failed_call_is_logged_before_falling_back(tmp_path, caplog):
    """`ok=False` still carries what the model actually said. Losing it means a
    bad recommendation cannot be reconstructed afterwards."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path,
        [
            failed_result(text="I would take the running back", error="timeout"),
            failed_result(text="still the running back", error="timeout"),
        ],
    )

    with caplog.at_level(logging.WARNING):
        advise(conn, settings, runner, bus, next_overall_pick=6)

    assert "I would take the running back" in caplog.text


# --- context -----------------------------------------------------------------


def test_notes_are_retrieved_by_query_not_by_exact_player_name(tmp_path, monkeypatch):
    """`players=` is an exact-ish match on a normalised name. The board comes from
    web research and the picks come from ESPN, so the two spell names differently
    often enough that the forgiving `query=` is the only one that finds anything.
    """
    seen: list[dict] = []
    real = memory.search_notes

    def spy(conn, query=None, **kwargs):
        seen.append({"query": query, **kwargs})
        return real(conn, query, **kwargs)

    monkeypatch.setattr(memory, "search_notes", spy)
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    advise(conn, settings, runner, bus, next_overall_pick=6)

    assert seen, "the advisor retrieves notes at all"
    assert seen[0]["query"], "notes are retrieved with a query"
    assert seen[0].get("players") is None, "players= would miss a differently-spelled name"
    assert "Chase" in seen[0]["query"], "the query names the leading candidates"


def test_live_state_travels_in_extra_context_not_glued_onto_the_prompt(tmp_path):
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    advise(conn, settings, runner, bus, next_overall_pick=6)
    call = runner.calls[0]

    assert "Ja'Marr Chase" in call["extra_context"]
    assert "Ja'Marr Chase" not in call["prompt"]


def test_the_context_carries_everything_the_advice_depends_on(tmp_path):
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])
    conn.execute(
        "INSERT INTO draft_picks (overall_pick, team_id, player_id, player_name, seen_at) "
        "VALUES (5, 5, -1001, 'Ja''Marr Chase', '2026-09-07T00:00:00+00:00')"
    )
    conn.execute("UPDATE board SET drafted_by_team_id = 5 WHERE name = 'Ja''Marr Chase'")
    conn.commit()

    advise(conn, settings, runner, bus, next_overall_pick=6)
    context = runner.calls[0]["extra_context"]

    assert "Bijan Robinson" in context, "the top of the board by tier"
    assert "Ja'Marr Chase" in context, "the last few picks"
    assert "RB/WR/TE" in context, "her open starting slots, named as ESPN names them"
    assert "RB/WR/TE x1 — one extra starter who can be" in context, (
        "and glossed by its own name: Caroline does not know what a flex slot is, "
        "and explaining a slot this league does not have would confuse her further"
    )
    assert "FLEX" not in context, "this league's flex slot is not called FLEX"
    assert "6, 7" in context or "6 and 7" in context, "both of her upcoming picks"


def test_the_schema_is_the_shape_the_draft_page_renders(tmp_path):
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    advise(conn, settings, runner, bus, next_overall_pick=6)
    schema = runner.calls[0]["schema"]

    assert set(schema["properties"]) == {"pick", "reason", "backups", "watch_out"}
    backup = schema["properties"]["backups"]["items"]["properties"]
    assert set(backup) == {"name", "reason"}


def test_the_prompt_files_are_real_deliverables():
    full = (REPO / "prompts" / "draft_advice.md").read_text(encoding="utf-8").lower()
    short = (REPO / "prompts" / "draft_advice_short.md").read_text(encoding="utf-8").lower()

    assert len(full) > 1200
    assert "jargon" in full or "does not know" in full
    assert "tier" in full
    assert "back to back" in full or "two picks" in full
    # The retry exists to be fast. If it grows to the size of the full prompt it
    # has stopped being a retry.
    assert len(short) < len(full)
    assert "one sentence" in short or "short" in short
