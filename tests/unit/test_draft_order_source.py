"""One draft order, read by the page, the advice card and the loop.

``draftSettings.orderType`` on this league is ``DRAFT_START``: ESPN draws the
real draft order **at the moment the draft opens**, and the ``pickOrder`` a
pre-draft sync stored is a placeholder. Nothing re-runs ``sync_league`` during a
draft, so that placeholder is frozen for the whole night.

That makes the divergence between the two sources the only interesting state
there is, and it is the state that had never been exercised: every fake in this
suite built its draft board by snaking the same identity order the placeholder
holds, so the two sources had literally never disagreed in any test.
:data:`~draft_fixtures.SHUFFLED_DRAFT_ORDER` and
:data:`~draft_fixtures.DIVERGENT_SCHEDULE` are the fixture that makes them
disagree, and this module is what that fixture is for.

**Why the disagreement is worse than an ordinary bug.** The page derives her pick
window from snake arithmetic over ``league.draft_order``; the advisor recomputes
the same arithmetic from the same field. So on divergence the two do not
contradict each other — they agree, and are wrong together, with no staleness
flag and nothing on screen to notice. She reads "4 picks until yours" while she
is on the clock, and the prompt asserts the same false position, so the
recommendation is reasoning from it too.

**The one thing that must not be done to fix it.** Handing the loop's
already-computed window to ``advise`` would make the card's label
schedule-derived while the page stayed arithmetic-derived — which is the
false-staleness bug Task 7b removed, reintroduced from the other side. The fix
is one source: the order ESPN drew is persisted on the first schedule read, and
all three read it back through ``load_league_context``.
"""

from __future__ import annotations

import json
import logging

from draft_fixtures import (
    DIVERGENT_SCHEDULE,
    PLACEHOLDER_NEXT_PICK,
    PLACEHOLDER_PICKS_AWAY,
    REAL_DRAFT_ORDER,
    REAL_MY_TEAM_ID,
    SAMPLE_BOARD,
    SHUFFLED_DRAFT_ORDER,
    SHUFFLED_NEXT_PICK,
    SHUFFLED_PICK_AFTER,
    SHUFFLED_PICKS_AWAY,
    FakeEspnClient,
    FakeRunner,
    RecordingBus,
    make_settings,
    ok_result,
    open_db,
    picks_through,
    seed_board,
    seed_synced_league,
    snake_schedule,
)

from hal_mary import db
from hal_mary.draft import store
from hal_mary.draft.board import pick_slot
from hal_mary.draft.loop import DraftLoop
from hal_mary.league import load_league_context
from hal_mary.web.draft_page import draft_context

ADVICE = {
    "pick": "Bijan Robinson",
    "reason": "He is the best player left and you have nobody yet.",
    "backups": [{"name": "Brock Bowers", "reason": "Take the tight end if Robinson is gone."}],
    "watch_out": "He is on a bye in week 5, the one week his real team does not play.",
}


def divergent_draft(tmp_path, *, picks=1, schedule=DIVERGENT_SCHEDULE):
    """A league whose stored ``pickOrder`` and live ESPN board disagree.

    The picks are made in the *shuffled* order, because that is the order the
    draft is actually running in — building them from the placeholder would have
    the fixture telling two stories at once.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_board(conn, SAMPLE_BOARD)
    client = FakeEspnClient(picks_through(picks, SHUFFLED_DRAFT_ORDER), schedule=schedule)
    runner = FakeRunner(settings, [ok_result(ADVICE) for _ in range(4)])
    loop = DraftLoop(conn, settings, client, runner, RecordingBus())
    return conn, settings, loop, client, runner


def latest_card(conn) -> dict:
    row = conn.execute(
        "SELECT payload_json FROM advice ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no advice was written"
    return json.loads(row["payload_json"])


# --- the fixture itself ------------------------------------------------------


def test_the_fixture_makes_the_two_sources_actually_disagree():
    """The guard on everything else in this file.

    If ESPN's board and the stored pick order ever agree again — someone
    regenerates the schedule from ``REAL_DRAFT_ORDER``, say — every test below
    would keep passing while testing nothing at all. That is precisely how this
    bug survived Task 12: the fake's default schedule *was* the snake.
    """
    placeholder_owner = pick_slot(SHUFFLED_NEXT_PICK, REAL_DRAFT_ORDER, snake=True)
    espn_owner = next(
        slot["team_id"]
        for slot in DIVERGENT_SCHEDULE
        if slot["overall_pick"] == SHUFFLED_NEXT_PICK
    )

    assert placeholder_owner != espn_owner, "the fixture no longer exercises a divergence"
    assert espn_owner == REAL_MY_TEAM_ID, "pick 2 is hers under the order ESPN drew"
    assert (
        pick_slot(PLACEHOLDER_NEXT_PICK, REAL_DRAFT_ORDER, snake=True) == REAL_MY_TEAM_ID
    ), "and pick 6 is hers under the placeholder — the two numbers she could be told"


# --- one source, read by all three -------------------------------------------


async def test_the_page_the_card_and_the_loop_all_report_the_pick_espn_drew(tmp_path):
    """The whole task in one assertion block.

    Pick 2 is on the clock and, under the order ESPN drew at DRAFT_START, pick 2
    is hers. Under the frozen placeholder her next pick is 6 and four teams pick
    before her. Every one of the three components must say 2 and zero.
    """
    conn, settings, loop, _, _ = divergent_draft(tmp_path)

    result = await loop.run_once()

    page = draft_context(conn, settings)
    turn, card = page["turn"], latest_card(conn)

    assert result["advised"] is True
    assert loop._last_advised_pick == SHUFFLED_NEXT_PICK, "the loop advised for her real pick"

    assert turn["my_next_picks"][0] == SHUFFLED_NEXT_PICK
    assert turn["picks_until_mine"] == SHUFFLED_PICKS_AWAY
    assert turn["mine_now"] is True
    assert turn["on_the_clock_team_id"] == REAL_MY_TEAM_ID

    assert card["my_next_picks"] == [SHUFFLED_NEXT_PICK, SHUFFLED_PICK_AFTER]
    assert card["picks_until_mine"] == SHUFFLED_PICKS_AWAY

    # And the card is not quietly labelled for a pick the page is not showing.
    assert page["advice"]["written_for"] == SHUFFLED_NEXT_PICK
    assert page["advice"]["stale"] is False


async def test_the_advisors_prompt_carries_the_picks_espn_drew(tmp_path):
    """Asserted on the rendered context, not on the inputs.

    The prompt is what the model reasons from. A card that happens to carry the
    right numbers in its payload while the context block asserts "she is four
    picks away" is still advice built on a false position.
    """
    _, _, loop, _, runner = divergent_draft(tmp_path)

    await loop.run_once()

    context = runner.calls[0]["extra_context"]
    assert f"next two picks are {SHUFFLED_NEXT_PICK} and {SHUFFLED_PICK_AFTER}" in context
    assert "She is on the clock now" in context
    assert f"next two picks are {PLACEHOLDER_NEXT_PICK} and 7" not in context
    assert f"{PLACEHOLDER_PICKS_AWAY} other team(s) pick before she does" not in context


# --- before the draft opens --------------------------------------------------


async def test_before_any_schedule_read_nothing_changes(tmp_path):
    """The order is only final once the draft is running.

    Until the first real pick there is nothing to prefer, so the placeholder is
    still what every component reads — reading ESPN's pre-draft board early and
    storing it would cache a plausible-looking lie.
    """
    conn, settings, loop, client, _ = divergent_draft(tmp_path, picks=0)

    await loop.run_once()

    assert client.draft_schedule_calls == 0, "nothing has been drafted; the order is not final"
    assert store.stored_draft_order(conn) == []
    assert load_league_context(conn, settings).draft_order == REAL_DRAFT_ORDER
    assert draft_context(conn, settings)["turn"]["my_next_picks"][0] == PLACEHOLDER_NEXT_PICK


# --- and once it is stored ---------------------------------------------------


async def test_the_stored_order_survives_a_restart_and_a_later_read_cannot_flap_it(tmp_path):
    """Written once, when the draft opens. Never rewritten.

    A restart mid-draft reads the schedule again — and ESPN is unofficial enough
    that a second read answering differently is a real possibility. The order is
    drawn once, so it is stored once: a later read must not move her pick window
    while she is looking at it.
    """
    conn, settings, loop, _, _ = divergent_draft(tmp_path)
    await loop.run_once()
    assert store.stored_draft_order(conn) == SHUFFLED_DRAFT_ORDER

    # A restart: a new connection, a new loop, nothing remembered in memory —
    # and an ESPN that has changed its mind about who picks where.
    reopened = db.connect(tmp_path / "hal.db")
    assert store.stored_draft_order(reopened) == SHUFFLED_DRAFT_ORDER, "it survived the restart"

    restarted = DraftLoop(
        reopened,
        settings,
        FakeEspnClient(
            picks_through(3, SHUFFLED_DRAFT_ORDER),
            schedule=snake_schedule([2, 3, 4, 5, 6, 1]),
        ),
        FakeRunner(settings, [ok_result(ADVICE) for _ in range(4)]),
        RecordingBus(),
    )
    await restarted.run_once()

    assert store.stored_draft_order(reopened) == SHUFFLED_DRAFT_ORDER, "a later read cannot flap it"
    assert load_league_context(reopened, settings).draft_order == SHUFFLED_DRAFT_ORDER
    reopened.close()


def test_a_league_sync_cannot_wipe_the_order_espn_drew(tmp_path):
    """Why this lives in its own table rather than a ``league_settings`` column.

    ``_write_league_settings`` is an ``INSERT OR REPLACE`` of the whole row, so a
    column added there is erased by the next ``hal-mary sync`` — and re-synced
    with ESPN's stale pre-draft ``pickOrder``, which is exactly the value the
    stored order exists to override. Mid-draft, from the ``/sync`` button, that
    would silently undo the fix.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    store.store_draft_order(conn, SHUFFLED_DRAFT_ORDER)

    seed_synced_league(conn)  # the whole league_settings row, rewritten

    assert store.stored_draft_order(conn) == SHUFFLED_DRAFT_ORDER
    assert load_league_context(conn, settings).draft_order == SHUFFLED_DRAFT_ORDER


# --- and when ESPN's board is not an order at all ----------------------------


async def test_a_board_whose_first_round_is_not_an_order_is_not_stored(tmp_path, caplog):
    """Storing it would be worse than the placeholder it replaces.

    A first round that names a team twice puts two of Caroline's picks in one
    round and none in another — a countdown wrong in a way the snake arithmetic
    over a stale order never is. The write happens once and is never revised, so
    the check has to happen before it, not after.
    """
    conn, settings, loop, _, _ = divergent_draft(
        tmp_path, schedule=snake_schedule([1, 6, 6, 4, 3, 2])
    )

    with caplog.at_level(logging.WARNING):
        await loop.run_once()

    assert store.stored_draft_order(conn) == []
    assert "unusable first round" in caplog.text
    assert load_league_context(conn, settings).draft_order == REAL_DRAFT_ORDER


def test_a_stored_order_that_does_not_fit_the_league_is_ignored(tmp_path, caplog):
    """The stored order is a correction, not an authority.

    It is written once and never revised, so a row of the wrong length — a
    partial write, a hand-edited database, an ESPN board for some other league —
    would otherwise raise :class:`LeagueUnknown` for the rest of the draft and
    take the pick countdown, the advisor and the roster card down with it. A
    correction that does not fit the league is discarded, loudly.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    store.store_draft_order(conn, [1, 6, 5])

    with caplog.at_level(logging.WARNING):
        league = load_league_context(conn, settings)

    assert league.draft_order == REAL_DRAFT_ORDER, "back to the placeholder, not an exception"
    assert "3" in caplog.text and "6" in caplog.text
