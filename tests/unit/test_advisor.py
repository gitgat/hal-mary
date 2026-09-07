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
import re
import sqlite3
import time

from draft_fixtures import (
    REPO,
    SAMPLE_BOARD,
    FakeClock,
    FakeRunner,
    RecordingBus,
    failed_result,
    make_settings,
    ok_result,
    open_db,
    seed_board,
    seed_synced_league,
)

from hal_mary import claude_runner, memory
from hal_mary.draft.advisor import _fits, advise
from hal_mary.events import EventBus
from hal_mary.league import load_league_context

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


def advisor_ready(tmp_path, results, board=None, extra="", replace=None, clock=None):
    settings = make_settings(tmp_path, extra, replace)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_board(conn, SAMPLE_BOARD if board is None else board)
    return conn, settings, FakeRunner(settings, results, clock), RecordingBus()


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
    """No board at all is the worst case, and it still must not show nothing.

    What it says matters as much as that it says something. Sorting the open
    positions alphabetically puts `D/ST` first, so the worst-case card used to
    read "take the best available D/ST" — two terms Caroline does not know,
    recommending the one category nobody takes in the first round.
    """
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")], board=[]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert result["source"] == "fallback"
    assert conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0] == 1

    card = f"{result['pick']} {result['reason']} {result['watch_out']}"
    assert "running back" in card or "receiver" in card, (
        "the worst-case card names a position she would actually draft here"
    )
    for jargon in ("D/ST", "RB", "WR", "TE", "QB", " K "):
        assert jargon not in card, f"{jargon!r} means nothing to someone who has never played"


def test_no_position_code_can_reach_the_card_unglossed():
    """The uppercase scan in the test above is weaker than it looks: an unglossed
    code was lowercased on its way out, so a league with a defensive-player slot
    would have said "take the best available dl" and the scan would have missed
    it. Every position any roster slot can name is glossed, and anything else
    falls back to words rather than to a lowercased code.
    """
    from hal_mary.draft.advisor import _POSITION_WORDS, _best_open_position
    from hal_mary.draft.board import _MULTI_POSITION_SLOTS

    reachable = {position for group in _MULTI_POSITION_SLOTS.values() for position in group}
    reachable |= {"QB", "RB", "WR", "TE", "K", "D/ST"}
    missing = sorted(position for position in reachable if position not in _POSITION_WORDS)
    assert missing == [], f"these positions have no plain-English word: {missing}"

    # And a code nobody anticipated still comes out as words, not as "zz".
    assert _best_open_position(["ZZ"]) == "player at any position"


def test_the_empty_board_card_never_leads_with_a_kicker_or_a_defence(tmp_path):
    """Even when they are the only slots open, they are not the first-round
    answer — and they are the two that win an alphabetical sort."""
    _, _, _, _ = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")], board=[]
    )

    from hal_mary.draft.advisor import _fallback

    card = _fallback({"open_positions": ["D/ST", "K", "QB", "RB", "TE", "WR"], "candidates": []})

    assert "running back" in card["pick"]


def test_advice_survives_a_failure_outside_the_model_call(tmp_path, monkeypatch):
    """The fallback exists so Caroline never sees an empty card. It only delivers
    on that if everything *around* the model call is covered too — a SQLite error
    while reading the board would otherwise escape, and the draft loop would
    swallow it and show her nothing at all for that pick."""
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("hal_mary.draft.advisor.store.load_board", explode)

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert result["source"] == "fallback"
    assert result["pick"]
    assert result["reason"]
    assert [event for event, _ in bus.published] == ["advice"]
    assert conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0] == 1


def test_a_card_is_still_returned_when_it_cannot_even_be_saved(tmp_path, monkeypatch):
    """Persisting is bookkeeping. Losing the row is bad; losing the card while a
    clock runs is worse, so the publish and the return do not depend on it."""
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("hal_mary.draft.advisor._persist", explode)

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert result["pick"] == "Saquon Barkley"
    assert result["advice_id"] is None
    assert [event for event, _ in bus.published] == ["advice"]


def test_the_retry_has_a_shorter_deadline_than_the_first_attempt(tmp_path):
    """One 45-second budget served both attempts, so two timeouts ate the whole
    90-second pick clock before the deterministic card could render."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")]
    )

    advise(conn, settings, runner, bus, next_overall_pick=6)

    first, second = (settings.job(call["job"]) for call in runner.calls)
    assert second.timeout_s < first.timeout_s
    assert second.tools == [], "the retry runs on the pick clock too"
    assert first.timeout_s + second.timeout_s <= 60, (
        "both attempts must time out with real time left on a 90-second clock"
    )


def test_the_number_of_recent_picks_shown_comes_from_config(tmp_path):
    """`advice_recent_picks` was a config key nothing read, which is exactly the
    kind of dead knob CLAUDE.md rule 5 exists to prevent."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path,
        [ok_result(ADVICE)],
        replace={"advice_recent_picks = 8": "advice_recent_picks = 3"},
    )
    assert settings.draft.advice_recent_picks == 3
    for overall in range(1, 9):
        conn.execute(
            "INSERT INTO draft_picks (overall_pick, team_id, player_name, seen_at) "
            "VALUES (?, 1, ?, '2026-09-07T00:00:00+00:00')",
            (overall, f"Filler {overall}"),
        )
    conn.commit()

    advise(conn, settings, runner, bus, next_overall_pick=9)
    context = runner.calls[0]["extra_context"]

    shown = len(re.findall(r"^- Pick \d+: ", context, re.MULTILINE))
    assert shown == settings.draft.advice_recent_picks


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


def test_the_fallback_explains_its_own_jargon(tmp_path):
    """Every other reason string is written by a model that was told not to use
    jargon. This one is written by Python, and it goes straight onto Caroline's
    card with no surrounding context — so the one word it cannot avoid, "tier",
    has to explain itself here.
    """
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), failed_result(error="timeout")]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert "tier" in result["reason"].lower()
    assert "interchangeable" in result["reason"].lower() or (
        "group" in result["reason"].lower()
    ), "a bare tier number means nothing to someone who has never drafted"
    for backup in result["backups"]:
        assert "tier" not in backup["reason"].lower() or "group" in backup["reason"].lower()


# --- the time budget ---------------------------------------------------------

def test_the_configured_budget_fits_inside_one_pick_clock(tmp_path):
    """The worked sum, pinned so it cannot drift.

    Sizing this by arithmetic in a comment is what let a 55-second figure stand
    while the real worst case was 94. The numbers that make it up are asserted
    here instead, against the shipped `config.toml`, including the teardown the
    runner spends *after* a deadline expires and the ESPN reads that run earlier
    in the same tick.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    # Read from the league, not written down here: a league that shortened its
    # clock has to fail this test rather than quietly overrun the new one.
    pick_clock = load_league_context(conn, settings).pick_clock_s
    assert pick_clock, "the pick clock is what every budget below is sized against"

    teardown = claude_runner.TIMEOUT_TEARDOWN_S
    first = settings.job("draft_advice").timeout_s
    retry = settings.job("draft_advice_retry").timeout_s
    espn_worst = settings.espn.connect_timeout_s + settings.espn.read_timeout_s

    # Both attempts timing out, each paying its teardown, must fit the budget.
    assert first + teardown + retry + teardown <= settings.draft.advice_budget_s

    # And the budget bounds the whole tick, sync included, with room left on the
    # clock for the card to render and be read.
    assert espn_worst <= settings.draft.advice_budget_s
    assert settings.draft.advice_budget_s <= pick_clock - 25


def test_an_attempt_costs_its_timeout_plus_the_runners_teardown(tmp_path):
    """The mistake this pins: budgeting on `timeout_s` alone.

    When a deadline expires the runner still has to kill the process group and
    reap it, then join the stdout reader — seven seconds that sit *outside*
    `timeout_s`. Leaving them out is how a 55-second worst case turned out to be
    69, so a window big enough for the deadline but not for the teardown must
    read as "does not fit".
    """
    settings = make_settings(tmp_path)
    budget = settings.job("draft_advice_retry").timeout_s

    now = time.monotonic()
    assert not _fits(settings, "draft_advice_retry", now + budget + 1), (
        "room for the deadline but not the teardown is not room"
    )
    assert _fits(settings, "draft_advice_retry", now + budget + claude_runner.TIMEOUT_TEARDOWN_S + 1)


def test_a_job_missing_from_an_older_config_degrades_instead_of_raising(tmp_path):
    settings = make_settings(tmp_path)

    assert not _fits(settings, "no_such_job", time.monotonic() + 10_000)


def test_an_attempt_that_cannot_finish_in_time_is_not_started(tmp_path):
    """A slow ESPN read earlier in the tick has already spent the clock. Starting
    a call that cannot return before the pick is due buys nothing and costs the
    card."""
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)])

    result = advise(
        conn, settings, runner, bus, next_overall_pick=6, deadline=time.monotonic() - 1
    )

    assert runner.calls == [], "no budget left, so no call was made"
    assert result["source"] == "fallback"
    assert result["pick"], "and she still gets a card"
    assert result["attempts"] == 0


def test_a_slow_sync_costs_the_retry_rather_than_the_card(tmp_path, monkeypatch):
    """Twenty-five seconds of ESPN, then a first attempt that times out, leaves no
    room for a second slow call. The budget spends what is left on the card that
    always renders, not on another attempt that would land after the pick."""
    clock = FakeClock()
    conn, settings, runner, bus = advisor_ready(
        tmp_path,
        [failed_result(error="timed out after 25.0s"), ok_result(ADVICE)],
        clock=clock,
    )
    monkeypatch.setattr("hal_mary.draft.advisor.time", clock)
    started = clock.monotonic()
    deadline = started + settings.draft.advice_budget_s
    clock.advance(settings.espn.connect_timeout_s + settings.espn.read_timeout_s)

    result = advise(conn, settings, runner, bus, next_overall_pick=6, deadline=deadline)

    assert len(runner.calls) == 1, "the retry could not have finished in time"
    assert result["source"] == "fallback"
    assert clock.monotonic() <= deadline, "and the whole thing stayed inside the budget"


def test_both_attempts_run_after_a_fast_sync(tmp_path, monkeypatch):
    """The retry earns its place when there is room: a quick sync leaves the whole
    budget, and a first attempt that timed out still leaves enough for the much
    smaller retry prompt."""
    clock = FakeClock()
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timed out"), ok_result(ADVICE)], clock=clock
    )
    monkeypatch.setattr("hal_mary.draft.advisor.time", clock)
    deadline = clock.monotonic() + settings.draft.advice_budget_s
    clock.advance(1)

    result = advise(conn, settings, runner, bus, next_overall_pick=6, deadline=deadline)

    assert len(runner.calls) == 2
    assert result["source"] == "claude"
    assert clock.monotonic() <= deadline


def test_two_timeouts_after_a_fast_sync_still_fit_the_budget(tmp_path, monkeypatch):
    """The worst case the config is sized for."""
    clock = FakeClock()
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timed out"), failed_result(error="timed out")],
        clock=clock,
    )
    monkeypatch.setattr("hal_mary.draft.advisor.time", clock)
    deadline = clock.monotonic() + settings.draft.advice_budget_s
    clock.advance(1)

    result = advise(conn, settings, runner, bus, next_overall_pick=6, deadline=deadline)

    assert len(runner.calls) == 2
    assert result["source"] == "fallback"
    assert result["pick"]
    assert clock.monotonic() <= deadline


def test_no_deadline_means_no_budget_gate(tmp_path):
    """Called outside the draft loop — a page refresh, a test — there is no tick
    to be inside, so the attempts run on their own timeouts alone."""
    conn, settings, runner, bus = advisor_ready(
        tmp_path, [failed_result(error="timeout"), ok_result(ADVICE)]
    )

    result = advise(conn, settings, runner, bus, next_overall_pick=6)

    assert len(runner.calls) == 2
    assert result["source"] == "claude"


def test_a_budget_too_small_for_the_first_attempt_still_runs_the_retry(tmp_path, monkeypatch):
    """The retry is a cheaper call, so a budget that cannot afford the full
    attempt can still afford it.

    With 25 + 7 for the first attempt and 15 + 7 for the retry, any remaining
    budget in [22, 32) fits the retry and not the first. Giving up there hands
    Caroline a ranked-list card with twenty-odd seconds unspent — the fallback
    firing while its own budget sat unused. That window is reachable on any tick
    whose pre-advisor spend lands between 28 and 38 seconds: a slow sync plus the
    one-off schedule read, or a tick that also warms the client.
    """
    clock = FakeClock()
    conn, settings, runner, bus = advisor_ready(tmp_path, [ok_result(ADVICE)], clock=clock)
    monkeypatch.setattr("hal_mary.draft.advisor.time", clock)

    first = settings.job("draft_advice").timeout_s + claude_runner.TIMEOUT_TEARDOWN_S
    retry = settings.job("draft_advice_retry").timeout_s + claude_runner.TIMEOUT_TEARDOWN_S
    assert retry < first, "the window this test lives in has to exist"
    deadline = clock.monotonic() + (first + retry) / 2  # inside [retry, first)

    result = advise(conn, settings, runner, bus, next_overall_pick=6, deadline=deadline)

    assert [call["job"] for call in runner.calls] == ["draft_advice_retry"], (
        "the attempt that did not fit is skipped; the one that fits is not"
    )
    assert result["source"] == "claude"
    assert result["attempts"] == 1
