"""Tests for the draft loop — the thing that is actually running on draft night.

It polls ESPN every five seconds for three hours. The properties that matter are
all about what must *not* happen over those three hours:

* **The advisor fires once per upcoming pick, not once per poll.** Without that,
  a five-second poll runs the advisor a dozen times per turn, burning money and
  flooding the page with cards that disagree with each other.
* **Nothing kills the loop.** An ESPN failure is logged and the loop lives. An
  exception escaping mid-draft is the failure with no recovery.
* **An unmatched pick is surfaced, not swallowed.** It means the board and
  reality disagree about who is gone, which is the one thing that makes a
  recommendation actively wrong.
* **The client is warmed before the first poll.** ``draft_picks()`` only builds
  its player-name map when the pick list is non-empty, so an unwarmed client
  does its slow full-league fetch on the very poll that sees pick 1 — mid-draft,
  on the clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from draft_fixtures import (
    SAMPLE_BOARD,
    FakeClock,
    FakeEspnClient,
    FakeRunner,
    RecordingBus,
    espn_pick,
    make_settings,
    ok_result,
    open_db,
    picks_through,
    seed_board,
    seed_synced_league,
    snake_schedule,
)

from hal_mary.draft import store
from hal_mary.draft.loop import (
    PHASE_DONE,
    PHASE_IDLE,
    PHASE_LIVE,
    DraftLoop,
    record_manual_pick,
)
from hal_mary.espn.client import EspnUnavailable

ADVICE = {
    "pick": "Bijan Robinson",
    "reason": "He is the best player left and you have nobody yet.",
    "backups": [{"name": "Brock Bowers", "reason": "Take the tight end if Robinson is gone."}],
    "watch_out": "He is on a bye in week 5, the one week his real team does not play.",
}


def loop_ready(tmp_path, *, picks=None, advice_results=8, board=None, replace=None, **kwargs):
    settings = make_settings(tmp_path, replace=replace)
    conn = open_db(tmp_path)
    seed_synced_league(conn, **kwargs)
    seed_board(conn, SAMPLE_BOARD if board is None else board)
    client = FakeEspnClient(picks or [])
    runner = FakeRunner(settings, [ok_result(ADVICE) for _ in range(advice_results)])
    bus = RecordingBus()
    return conn, DraftLoop(conn, settings, client, runner, bus), client, bus, runner


def advice_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0]


# --- startup -----------------------------------------------------------------


async def test_the_client_is_warmed_before_the_first_poll(tmp_path):
    """The slow full-league fetch happens before the draft, not on the poll that
    sees pick 1."""
    _, loop, client, _, _ = loop_ready(tmp_path)

    await loop.run_once()

    assert client.call_order[0] == "player_name_map"
    assert client.name_map_calls == 1


async def test_the_client_is_warmed_only_once(tmp_path):
    _, loop, client, _, _ = loop_ready(tmp_path)

    await loop.run_once()
    await loop.run_once()

    assert client.name_map_calls == 1
    assert client.draft_picks_calls == 2


async def test_a_failed_warm_does_not_stop_the_loop(tmp_path):
    """A transient ESPN failure at startup costs names, not the draft."""
    _, loop, client, _, _ = loop_ready(tmp_path)

    def explode(refresh: bool = False):
        client.name_map_calls += 1
        client.call_order.append("player_name_map")
        raise EspnUnavailable("ESPN is having a moment")

    client.player_name_map = explode

    result = await loop.run_once()

    assert result["error"] is None
    assert client.draft_picks_calls == 1


# --- picks -------------------------------------------------------------------


async def test_a_new_pick_updates_the_board_and_publishes(tmp_path):
    conn, loop, _, bus, _ = loop_ready(tmp_path, picks=picks_through(1))

    result = await loop.run_once()

    assert result["new_picks"] == 1
    row = conn.execute(
        "SELECT drafted_by_team_id, drafted_at FROM board WHERE name = 'Ja''Marr Chase'"
    ).fetchone()
    assert row["drafted_by_team_id"] == 1
    assert row["drafted_at"], "a drafted row always carries a timestamp"
    assert "board_updated" in [event for event, _ in bus.published]


async def test_a_poll_with_no_new_picks_does_nothing(tmp_path):
    conn, loop, _, bus, _ = loop_ready(tmp_path, picks=picks_through(1))

    await loop.run_once()
    published_after_first = len(bus.published)
    await loop.run_once()

    assert len(bus.published) == published_after_first, "no board_updated for an idle poll"
    assert advice_count(conn) == 0


async def test_a_pick_with_no_name_is_shown_by_its_player_id(tmp_path):
    """ESPN's name map can be briefly unavailable. A blank card is worse than
    'player 4262921 is gone'."""
    _, loop, _, bus, _ = loop_ready(tmp_path, picks=[espn_pick(1, 1, 4262921, None)])

    await loop.run_once()

    payload = dict(bus.published)["board_updated"]
    assert payload["picks"][0]["label"] == "player 4262921"


async def test_an_unmatched_pick_is_logged_and_stored_for_the_page(tmp_path, caplog):
    """An unmatched pick means the board and reality disagree about who is gone.
    A line in a log file is not enough while a clock is running."""
    conn, loop, _, bus, _ = loop_ready(
        tmp_path, picks=[espn_pick(1, 1, 999999, "Nobody On The Board")]
    )

    with caplog.at_level(logging.WARNING):
        result = await loop.run_once()

    assert len(result["unmatched"]) == 1
    assert "Nobody On The Board" in caplog.text
    stored = store.unmatched_picks(conn)
    assert [row["player_name"] for row in stored] == ["Nobody On The Board"]
    assert dict(bus.published)["board_updated"]["unmatched"], "the page hears about it too"


async def test_an_unmatched_pick_is_stored_once_not_once_per_poll(tmp_path):
    conn, loop, _, _, _ = loop_ready(
        tmp_path, picks=[espn_pick(1, 1, 999999, "Nobody On The Board")]
    )

    await loop.run_once()
    await loop.run_once()

    assert len(store.unmatched_picks(conn)) == 1


# --- surviving ESPN ----------------------------------------------------------


async def test_an_espn_exception_is_swallowed_and_the_loop_survives(tmp_path, caplog):
    _, loop, client, bus, _ = loop_ready(tmp_path, picks=picks_through(1))
    client.fail_next = EspnUnavailable("ESPN returned 503")

    with caplog.at_level(logging.WARNING):
        first = await loop.run_once()

    assert first["error"] is not None
    assert "503" in caplog.text
    assert bus.published == [], "a failed poll publishes nothing"

    second = await loop.run_once()
    assert second["new_picks"] == 1, "the next poll picks up where it left off"


async def test_run_forever_keeps_polling_and_stops_when_told(tmp_path):
    """Waits for the second poll rather than for a fixed number of milliseconds.

    A fixed ``asyncio.sleep`` here is a race the machine wins under load: this
    test failed twice on a busy box while the code was fine. A flaky test on the
    draft path teaches people to re-run instead of read, which is how a real
    failure gets waved through on the night.
    """
    _, loop, client, _, _ = loop_ready(
        tmp_path,
        picks=picks_through(1),
        replace={
            "poll_seconds = 5": "poll_seconds = 0",
            "idle_poll_seconds = 300": "idle_poll_seconds = 0",
        },
    )
    client.fail_next = EspnUnavailable("ESPN returned 503")

    task = asyncio.create_task(loop.run_forever())
    try:
        async with asyncio.timeout(5):
            while client.draft_picks_calls <= 1:
                await asyncio.sleep(0.005)
    except TimeoutError:
        pass  # let the assertion below say what was actually wrong
    finally:
        loop.stop()
        await asyncio.wait_for(task, timeout=5)

    assert client.draft_picks_calls > 1, "the failure did not end the loop"


# --- advising ----------------------------------------------------------------


async def test_the_advisor_fires_once_as_the_turn_approaches_not_once_per_poll(tmp_path):
    """Caroline picks 6th of 6. After three picks she is two away, which is the
    configured trigger — and a five-second poll must not fire it again."""
    conn, loop, client, _, runner = loop_ready(tmp_path, picks=picks_through(3))

    await loop.run_once()
    assert advice_count(conn) == 1
    assert len(runner.calls) == 1

    client.picks = picks_through(4)
    await loop.run_once()
    assert advice_count(conn) == 1, "still the same upcoming pick; do not advise again"

    client.picks = picks_through(5)
    await loop.run_once()
    assert advice_count(conn) == 1
    assert len(runner.calls) == 1


async def test_the_advisor_does_not_fire_while_her_turn_is_far_away(tmp_path):
    conn, loop, _, bus, _ = loop_ready(tmp_path, picks=picks_through(1))

    await loop.run_once()

    assert advice_count(conn) == 0
    assert [event for event, _ in bus.published] == ["board_updated", "draft_phase"], (
        "the pick, and the loop moving to draft-night cadence because of it"
    )


async def test_the_advisor_fires_again_for_her_next_turn(tmp_path):
    conn, loop, client, _, _ = loop_ready(tmp_path, picks=picks_through(3))

    await loop.run_once()
    assert advice_count(conn) == 1

    # Picks 4 through 8 happen, including both of hers; her next turn is 18, and
    # by pick 16 she is two away again.
    client.picks = [
        espn_pick(index, (index - 1) % 6 + 1, -2000 - index, f"Filler {index}")
        for index in range(1, 17)
    ]
    await loop.run_once()

    assert advice_count(conn) == 2


async def test_no_advice_once_the_draft_is_over(tmp_path):
    """`picks_until_mine` treats the draft as infinite and would happily count
    down forever. The end-of-draft signal is an empty upcoming-picks list."""
    conn, loop, client, _, _ = loop_ready(
        tmp_path, roster_slots_json=json.dumps({"QB": 1, "RB": 1})
    )
    # Two rounds, six teams: twelve picks and the draft is done.
    client.schedule = snake_schedule([1, 2, 3, 4, 5, 6], rounds=2)
    client.picks = [
        espn_pick(index, (index - 1) % 6 + 1, -3000 - index, f"Filler {index}")
        for index in range(1, 13)
    ]

    result = await loop.run_once()

    assert result["draft_over"] is True
    assert advice_count(conn) == 0


# --- manual picks ------------------------------------------------------------


async def test_a_manual_pick_flows_through_the_same_path(tmp_path):
    """ESPN going down at the worst possible moment is not a reason to stop."""
    conn, _, _, bus, _ = loop_ready(tmp_path)

    result = record_manual_pick(conn, bus=bus, player_name="Ja'Marr Chase")

    assert result["recorded"] is True
    row = conn.execute(
        "SELECT drafted_at FROM board WHERE name = 'Ja''Marr Chase'"
    ).fetchone()
    assert row["drafted_at"], "a hand-entered pick knows the player, not the team"
    assert store.next_overall_pick(conn) == 2, "it takes a real pick number"
    assert "board_updated" in [event for event, _ in bus.published]


async def test_a_hand_entered_player_stays_gone_after_a_reload(tmp_path):
    """The board table has no `drafted` column, only a team id — and a manual
    pick has no team. Without a timestamp he would come back as available on the
    next poll and be recommended again."""
    conn, _, _, _, _ = loop_ready(tmp_path)

    record_manual_pick(conn, player_name="Ja'Marr Chase")
    board = store.load_board(conn)

    gone = next(row for row in board if row["name"] == "Ja'Marr Chase")
    assert gone["drafted"] is True


async def test_the_same_player_entered_twice_is_one_pick(tmp_path):
    """A double tap on the manual button must not become a second pick, and must
    not come back as an unmatched pick on every poll forever."""
    conn, _, _, bus, _ = loop_ready(tmp_path)

    first = record_manual_pick(conn, bus=bus, player_name="Ja'Marr Chase")
    second = record_manual_pick(conn, bus=bus, player_name="Ja'Marr Chase")

    assert first["recorded"] is True
    assert second["recorded"] is False
    assert conn.execute("SELECT COUNT(*) FROM draft_picks").fetchone()[0] == 1
    assert store.unmatched_picks(conn) == []


async def test_espn_reporting_a_hand_entered_pick_adds_its_team_rather_than_a_warning(tmp_path):
    """The realistic sequence: Caroline taps 'taken' while ESPN is slow, and ESPN
    catches up a minute later with the same player and the team that took him."""
    conn, loop, client, bus, _ = loop_ready(tmp_path)
    record_manual_pick(conn, bus=bus, player_name="Ja'Marr Chase")

    client.picks = [espn_pick(1, 3, -1001, "Ja'Marr Chase")]
    await loop.run_once()

    assert store.unmatched_picks(conn) == []
    row = conn.execute(
        "SELECT drafted_by_team_id FROM board WHERE name = 'Ja''Marr Chase'"
    ).fetchone()
    assert row["drafted_by_team_id"] == 3


async def test_two_reports_of_one_player_in_a_single_batch_are_deduplicated(tmp_path):
    """Whichever arrived second would come back unmatched — on every poll, for
    the rest of the draft."""
    conn, loop, client, _, _ = loop_ready(tmp_path)
    client.picks = [
        espn_pick(1, 1, -1001, "Ja'Marr Chase"),
        espn_pick(2, 2, -9999, "Ja'Marr Chase"),
    ]

    result = await loop.run_once()

    assert result["unmatched"] == []
    assert result["duplicates"] == 1
    assert store.unmatched_picks(conn) == []


async def test_a_manual_pick_the_loop_can_reach_is_available_on_the_loop(tmp_path):
    """The draft page holds a loop, not a module. It must not have to import the
    function separately to record a pick."""
    conn, loop, _, _, _ = loop_ready(tmp_path)

    result = loop.record_manual_pick(player_name="Bijan Robinson", team_id=4)

    assert result["recorded"] is True
    row = conn.execute(
        "SELECT drafted_by_team_id FROM board WHERE name = 'Bijan Robinson'"
    ).fetchone()
    assert row["drafted_by_team_id"] == 4


async def test_a_manual_pick_for_someone_not_on_the_board_is_still_recorded(tmp_path):
    conn, _, _, bus, _ = loop_ready(tmp_path)

    result = record_manual_pick(conn, bus=bus, player_name="Some Unranked Kicker")

    assert result["recorded"] is True
    assert [row["player_name"] for row in store.unmatched_picks(conn)] == [
        "Some Unranked Kicker"
    ]


async def test_espns_pre_populated_placeholder_picks_are_ignored(tmp_path):
    """ESPN fills all 96 picks in before the draft starts, every one of them with
    `playerId: -1` and no name (confirmed from the live payload on 2026-09-07).

    A pick that identifies nobody cannot match a board row, so applying it would
    file 96 unmatched-pick warnings; and counting it would put the next pick at
    97, which reads as "the draft is over" before it has begun. Task 12 filters
    these at the client layer — this is the second lock on the same door, because
    the failure is total and silent.
    """
    conn, loop, _, bus, _ = loop_ready(
        tmp_path,
        picks=[
            {
                "overall_pick": index,
                "round_num": (index - 1) // 6 + 1,
                "round_pick": (index - 1) % 6 + 1,
                "team_id": (index - 1) % 6 + 1,
                "player_id": -1,
                "player_name": None,
            }
            for index in range(1, 97)
        ],
    )

    result = await loop.run_once()

    assert store.unmatched_picks(conn) == []
    assert store.next_overall_pick(conn) == 1, "nobody has actually been drafted yet"
    assert result["draft_over"] is False
    assert advice_count(conn) == 0
    assert bus.published == []


# --- the pick schedule -------------------------------------------------------


async def test_the_schedule_is_read_when_the_draft_opens_and_not_before(tmp_path):
    """`draftSettings.orderType` is DRAFT_START: ESPN assigns the real order when
    the draft begins, so the board it pre-populates is a provisional lie. Reading
    it early and caching it would be worse than not reading it at all."""
    _, loop, client, _, _ = loop_ready(tmp_path)

    await loop.run_once()
    assert client.draft_schedule_calls == 0, "nothing has been drafted; the order is not final"

    client.picks = picks_through(1)
    await loop.run_once()
    assert client.draft_schedule_calls == 1, "the draft opened; read the real order now"

    client.picks = picks_through(2)
    await loop.run_once()
    assert client.draft_schedule_calls == 1, "and only once: it does not change again"


async def test_the_schedule_beats_the_pick_order_the_sync_cached(tmp_path):
    """ESPN's own board says who owns which pick. Deriving it from a provisional
    pick order and a snake rule is how the countdown ends up off by five."""
    conn, loop, client, _, _ = loop_ready(tmp_path, picks=picks_through(1))
    # ESPN shuffled at DRAFT_START: Caroline is second now, not last.
    client.schedule = snake_schedule([1, 6, 5, 4, 3, 2])

    await loop.run_once()

    # By the cached pick order she owns pick 6 and is four away, which is outside
    # the trigger. By ESPN's real board pick 2 is hers and she is on the clock.
    assert advice_count(conn) == 1


async def test_a_schedule_that_cannot_be_read_falls_back_to_the_arithmetic(tmp_path):
    conn, loop, client, _, _ = loop_ready(tmp_path, picks=picks_through(3))
    client.fail_schedule = EspnUnavailable("ESPN returned 503")

    result = await loop.run_once()

    assert result["error"] is None
    assert advice_count(conn) == 1, "the snake arithmetic still says she is two away"


# --- surviving everything else -----------------------------------------------


async def test_a_failure_inside_the_advisor_path_does_not_kill_the_loop(tmp_path, monkeypatch):
    """The catch after the sync had no test: every other failure test drives ESPN,
    which the first handler catches, so deleting this one kept the suite green."""
    _, loop, client, _, _ = loop_ready(tmp_path, picks=picks_through(3))

    def explode(*args, **kwargs):
        raise RuntimeError("the advisor exploded on its way out")

    monkeypatch.setattr("hal_mary.draft.loop.advise", explode)

    first = await loop.run_once()

    assert "exploded" in first["error"], "the tick reports what went wrong"
    assert first["new_picks"] == 3, "and everything before the failure still happened"

    # The loop is alive and the board work of the next poll still lands.
    client.picks = picks_through(4)
    second = await loop.run_once()
    assert second["error"] is None


async def test_an_empty_board_does_not_file_a_warning_for_every_pick(tmp_path):
    """With no board, nothing can match, so every pick is 'unmatched' — up to 95
    warnings for the draft page to render, none of which mean what the warning
    means. The real problem is the missing board, and it is one problem."""
    conn, loop, _, bus, _ = loop_ready(tmp_path, picks=picks_through(8), board=[])

    result = await loop.run_once()

    assert store.unmatched_picks(conn) == []
    assert result["unmatched"] == []
    assert dict(bus.published)["board_updated"]["board_missing"] is True


async def test_the_tick_budget_starts_before_the_sync_not_after_it(tmp_path, monkeypatch):
    """A slow ESPN read must come out of the same allowance as the Claude calls.

    The read is bounded at 25 seconds by the ESPN timeouts and it runs *earlier
    in the same tick*. Starting the budget after it would let a slow sync plus
    two slow attempts overrun the 90-second pick clock — the card would arrive
    after the pick was made, which is the exact failure the budget exists to
    prevent. So the deadline is an instant fixed at the top of the tick, and a
    slow sync spends it like anything else.
    """
    clock = FakeClock()
    seen: dict[str, float] = {}

    def spy(conn, settings, runner, bus, *, next_overall_pick, deadline=None):
        seen["deadline"] = deadline
        return {"pick": "Somebody", "reason": "x", "backups": [], "watch_out": ""}

    monkeypatch.setattr("hal_mary.draft.loop.advise", spy)
    monkeypatch.setattr("hal_mary.draft.loop.time", clock)
    _, loop, client, _, _ = loop_ready(tmp_path, picks=picks_through(3))
    budget = loop.settings.draft.advice_budget_s
    espn_worst = loop.settings.espn.connect_timeout_s + loop.settings.espn.read_timeout_s

    client.clock, client.slow_by = clock, espn_worst
    tick_started = clock.monotonic()
    await loop.run_once()

    assert seen["deadline"] == tick_started + budget, (
        "the deadline is the top of the tick plus the budget, not the end of the sync"
    )
    # What the advisor is actually left with, which is the point of measuring it
    # from the top: a 25-second sync has already spent 25 of the 60.
    assert seen["deadline"] - clock.monotonic() == budget - espn_worst


# --- how often it polls ------------------------------------------------------
#
# The loop was written for draft night and left running forever: five seconds
# for a hundred and twenty days is 2,073,600 requests against an unofficial API
# for a job that needs about 2,160 of them, once. What bounds it is the loop
# knowing which of three phases it is in, and the phase coming from the board
# rather than from a flag ESPN may only set once the draft is over.


async def test_the_loop_idles_when_no_pick_has_been_made(tmp_path):
    """Before the draft there is nothing to watch every five seconds."""
    _, loop, _, _, _ = loop_ready(tmp_path)

    await loop.run_once()

    assert loop.phase == PHASE_IDLE
    assert loop.poll_interval == 300, "the idle cadence, not the draft-night one"


async def test_a_real_pick_puts_the_loop_on_draft_night_cadence(tmp_path):
    """One pick is the only signal that cannot be a pre-populated placeholder."""
    _, loop, _, _, _ = loop_ready(tmp_path, picks=picks_through(1))

    await loop.run_once()

    assert loop.phase == PHASE_LIVE
    assert loop.poll_interval == 5


async def test_a_full_board_stops_the_loop(tmp_path):
    """Every slot filled: there is nothing left to watch, ever."""
    _, loop, client, _, _ = loop_ready(tmp_path)
    client.picks = picks_through(len(SAMPLE_BOARD))
    client.slots = len(SAMPLE_BOARD)

    await loop.run_once()

    assert loop.phase == PHASE_DONE
    assert loop.poll_interval is None, "a stopped loop has no cadence at all"


async def test_espns_drafted_flag_does_not_stop_a_partly_filled_draft(tmp_path, caplog):
    """The flag ESPN may only set once the draft is over does not get to end it.

    ``docs/DECISIONS.md`` records that ``draftDetail.drafted`` is why the raw
    endpoint exists at all. A phase detector that believed it would stop polling
    mid-draft — the one direction in which being wrong costs Caroline picks.
    """
    _, loop, client, _, _ = loop_ready(tmp_path, picks=picks_through(3))
    client.drafted = True

    with caplog.at_level(logging.WARNING):
        await loop.run_once()

    assert loop.phase == PHASE_LIVE
    assert loop.poll_interval == 5
    assert "drafted" in caplog.text.lower(), "the disagreement is worth a line"


async def test_in_progress_before_the_first_pick_is_not_a_live_draft(tmp_path):
    """The real pre-draft payload's own flags must not start the fast clock.

    ESPN pre-populates all 96 slots and carries ``inProgress`` alongside them;
    a detector that trusted the flag would poll every five seconds from the day
    the league was created, which is the bug this phase work exists to remove.
    """
    _, loop, client, _, _ = loop_ready(tmp_path)
    client.in_progress = True
    client.drafted = False

    await loop.run_once()

    assert loop.phase == PHASE_IDLE
    assert loop.poll_interval == 300


async def test_a_phase_change_is_logged_and_published(tmp_path, caplog):
    _, loop, client, bus, _ = loop_ready(tmp_path)

    with caplog.at_level(logging.INFO):
        await loop.run_once()
        client.picks = picks_through(1)
        await loop.run_once()

    published = [payload for event, payload in bus.published if event == "draft_phase"]
    assert [entry["phase"] for entry in published] == [PHASE_LIVE], (
        "a change is published; a phase that did not change is not news"
    )
    assert published[-1]["poll_seconds"] == 5
    assert "300s" in caplog.text and "5s" in caplog.text, "the log names both cadences"
    assert "live" in caplog.text and "idle" in caplog.text


async def test_a_phase_change_takes_effect_without_restarting_the_loop(tmp_path):
    """The cadence actually used, tick by tick — not the config value."""
    _, loop, client, _, _ = loop_ready(tmp_path)
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 1:
            # The draft opens between two polls, which is how it really happens.
            client.picks = picks_through(1)
        if len(waits) >= 3:
            loop.stop()

    loop._wait_for_next_poll = record

    await asyncio.wait_for(loop.run_forever(), timeout=5)

    assert waits == [300, 5, 5], "the cadence actually used, tick by tick"


async def test_the_loop_stops_polling_once_the_draft_is_over(tmp_path):
    _, loop, client, _, _ = loop_ready(tmp_path)
    client.picks = picks_through(len(SAMPLE_BOARD))
    client.slots = len(SAMPLE_BOARD)
    waits: list[float] = []

    async def record(seconds: float) -> None:  # pragma: no cover - must not run
        waits.append(seconds)

    loop._wait_for_next_poll = record

    await asyncio.wait_for(loop.run_forever(), timeout=5)

    assert waits == [], "a finished draft is not polled again"
    assert client.draft_picks_calls == 1


async def test_stop_wakes_the_loop_out_of_a_five_minute_idle_wait(tmp_path):
    """Shutdown must not wait out the idle interval.

    A systemd deploy stops the service with SIGTERM; if the loop only notices
    when its wait times out, every restart can leave a polling thread behind and
    they accumulate against the same unofficial API.
    """
    _, loop, _, _, _ = loop_ready(tmp_path)
    polled = asyncio.Event()
    original = loop.run_once

    async def watched():
        result = await original()
        polled.set()
        return result

    loop.run_once = watched

    task = asyncio.create_task(loop.run_forever())
    await asyncio.wait_for(polled.wait(), timeout=5)
    assert loop.poll_interval == 300, "it is parked on the idle wait"

    started = time.monotonic()
    await asyncio.to_thread(loop.stop)
    await asyncio.wait_for(task, timeout=5)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"stop took {elapsed:.1f}s of a 300s wait"


# --- the override ------------------------------------------------------------


async def test_the_draft_has_started_switches_to_live_at_once(tmp_path):
    """The button is an override: it must not wait for the next idle poll."""
    _, loop, _, bus, _ = loop_ready(tmp_path)

    await loop.run_once()
    assert loop.phase == PHASE_IDLE

    outcome = loop.draft_started()

    assert outcome["phase"] == PHASE_LIVE
    assert loop.poll_interval == 5
    assert [payload["phase"] for event, payload in bus.published if event == "draft_phase"][
        -1
    ] == PHASE_LIVE


async def test_the_override_survives_a_tick_that_still_sees_no_picks(tmp_path):
    """ESPN's board is empty between the draft opening and pick 1."""
    _, loop, _, _, _ = loop_ready(tmp_path)

    loop.draft_started()
    await loop.run_once()

    assert loop.phase == PHASE_LIVE, "the override must not be undone by the next tick"


async def test_the_override_expires_so_a_stray_tap_is_not_forever(tmp_path):
    """An accidental press must not restore the five-second-forever loop."""
    _, loop, _, _, _ = loop_ready(tmp_path)
    clock = FakeClock()
    loop._clock = clock

    loop.draft_started()
    await loop.run_once()
    assert loop.phase == PHASE_LIVE

    clock.advance(loop.settings.draft.live_override_seconds + 1)
    await loop.run_once()

    assert loop.phase == PHASE_IDLE


async def test_the_loop_reaches_live_with_no_button_press(tmp_path):
    """The button is an override, not the mechanism."""
    _, loop, client, _, _ = loop_ready(tmp_path)

    await loop.run_once()
    assert loop.phase == PHASE_IDLE

    client.picks = picks_through(1)
    await loop.run_once()

    assert loop.phase == PHASE_LIVE, "nobody pressed anything"
