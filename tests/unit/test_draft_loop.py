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

from draft_fixtures import (
    SAMPLE_BOARD,
    FakeEspnClient,
    FakeRunner,
    RecordingBus,
    make_settings,
    ok_result,
    open_db,
    seed_board,
    seed_synced_league,
)

from hal_mary.draft import store
from hal_mary.draft.loop import DraftLoop, record_manual_pick
from hal_mary.espn.client import EspnUnavailable

ADVICE = {
    "pick": "Bijan Robinson",
    "reason": "He is the best player left and you have nobody yet.",
    "backups": [{"name": "Brock Bowers", "reason": "Take the tight end if Robinson is gone."}],
    "watch_out": "He is on a bye in week 5, the one week his real team does not play.",
}


def espn_pick(overall: int, team_id: int, player_id: int, name: str | None = None) -> dict:
    return {
        "overall_pick": overall,
        "round_num": (overall - 1) // 6 + 1,
        "round_pick": (overall - 1) % 6 + 1,
        "team_id": team_id,
        "player_id": player_id,
        "player_name": name,
    }


#: The first few picks of the real draft, in board order, taken by other teams.
BOARD_ORDER = [row["name"] for row in SAMPLE_BOARD]


def picks_through(count: int) -> list[dict]:
    """``count`` picks, taking players off the top of the sample board."""
    return [
        espn_pick(index + 1, (index % 6) + 1, SAMPLE_BOARD[index]["player_id"], BOARD_ORDER[index])
        for index in range(count)
    ]


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
    _, loop, client, _, _ = loop_ready(
        tmp_path, picks=picks_through(1), replace={"poll_seconds = 5": "poll_seconds = 0"}
    )
    client.fail_next = EspnUnavailable("ESPN returned 503")

    task = asyncio.create_task(loop.run_forever())
    await asyncio.sleep(0.05)
    loop.stop()
    await asyncio.wait_for(task, timeout=2)

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
    assert [event for event, _ in bus.published] == ["board_updated"]


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
