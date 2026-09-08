"""Tests for the draft page — the screen Caroline reads with a clock running.

Everything here builds the real app over a temporary database through
``create_app``'s injection seams, so no test touches ``hal.db``, ESPN, or the
``claude`` binary. ``tests/conftest.py`` blocks both HTTP stacks.

The assertions are deliberately about *what she can see*, not about internal
shapes. A context dict with the right keys and a template that never printed
them is the exact failure this page cannot afford.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from draft_fixtures import (
    REAL_MY_TEAM_ID,
    SAMPLE_BOARD,
    make_settings,
    seed_board,
    seed_synced_league,
)
from fastapi.testclient import TestClient

from hal_mary import db

PASSWORD = "not-a-real-password"

#: Six teams in this league; team 6 is hers. Names are invented — CLAUDE.md
#: forbids a real leaguemate's name reaching a fixture.
FAKE_TEAMS = (
    (1, "Gridiron Gerbils", "GERB", 1),
    (2, "Punt Intended", "PUNT", 2),
    (3, "Team Three", "TM3", 3),
    (4, "The Invented Four", "INV4", 4),
    (5, "Marla's Marvellous Squad", "MMS", 5),
    (REAL_MY_TEAM_ID, "Hail Mary Hopefuls", "HMH", 6),
)


# --- plumbing ----------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "hal.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


@pytest.fixture
def settings(tmp_path: Path, db_path: Path):
    return make_settings(tmp_path, DB_PATH=str(db_path))


def open_conn(db_path: Path) -> sqlite3.Connection:
    return db.connect(db_path)


def build_app(db_path: Path, settings, **kwargs):
    from hal_mary.web.app import create_app

    kwargs.setdefault("check_auth", lambda: (True, "ESPN credentials are valid"))
    return create_app(settings, connect=lambda: open_conn(db_path), **kwargs)


def client_for(db_path: Path, settings, **kwargs) -> TestClient:
    return TestClient(build_app(db_path, settings, **kwargs), follow_redirects=False)


def signed_in(db_path: Path, settings, **kwargs) -> TestClient:
    client = client_for(db_path, settings, **kwargs)
    client.post("/login", data={"password": PASSWORD})
    return client


def csrf_of(client: TestClient, settings) -> str:
    """The token this browser would submit, read from its own cookie jar."""
    return client.cookies[settings.web.csrf_cookie]


def post(client: TestClient, settings, url: str, data: dict[str, Any], **kwargs):
    """A form post that carries the double-submit token, as the page does."""
    body = {"csrf_token": csrf_of(client, settings), **data}
    return client.post(url, data=body, **kwargs)


def seed_league(db_path: Path) -> None:
    conn = open_conn(db_path)
    seed_synced_league(conn)
    conn.executemany(
        "INSERT INTO teams (team_id, name, owner, abbrev, draft_slot, updated_at)"
        " VALUES (?, ?, NULL, ?, ?, '2026-09-07T00:00:00+00:00')",
        FAKE_TEAMS,
    )
    conn.commit()
    conn.close()


def seed_picks(db_path: Path, picks: list[tuple[int, int, str]]) -> None:
    """``(overall_pick, team_id, player_name)`` rows, as ESPN would leave them."""
    conn = open_conn(db_path)
    conn.executemany(
        "INSERT INTO draft_picks (overall_pick, team_id, player_id, player_name, seen_at)"
        " VALUES (?, ?, NULL, ?, '2026-09-07T00:00:00+00:00')",
        picks,
    )
    conn.commit()
    conn.close()


def seed_advice(db_path: Path, **payload: Any) -> None:
    """One ``advice`` row shaped exactly as ``advisor.advise`` leaves it."""
    full = {
        "pick": "Bijan Robinson",
        "reason": "He plays every down and catches passes, which is worth a lot here.",
        "backups": [
            {"name": "Saquon Barkley", "reason": "Scores a lot of touchdowns."},
            {"name": "Puka Nacua", "reason": "Gets thrown to constantly."},
        ],
        "watch_out": "He is coming back from a knock, so check he is playing.",
        "source": "claude",
        "attempts": 1,
        "next_overall_pick": 6,
        "picks_until_mine": 0,
        "my_next_picks": [6, 7],
        "board_built_at": "2026-09-07T00:00:00+00:00",
    }
    full.update(payload)
    conn = open_conn(db_path)
    conn.execute(
        "INSERT INTO advice (created_at, kind, headline, body, payload_json, source_job)"
        " VALUES (?, 'draft', ?, ?, ?, 'draft_advice')",
        (
            db.utc_now(),
            f"Pick {full['next_overall_pick']}: take {full['pick']}",
            full["reason"],
            json.dumps(full),
        ),
    )
    conn.commit()
    conn.close()


# --- fixtures built by running the loop --------------------------------------
#
# Everything about an advice card below — above all *which pick it is for* — is
# produced by the code that runs on draft night, not by a dict written here.
# A hand-written advice row encodes what the author believed the loop stores, so
# no assertion over it can contradict the belief that produced the bug: this
# file once asserted, and a browser once showed, a card/pick alignment the loop
# cannot actually produce.

#: Picks in board order, skipping Bijan Robinson so he is still there to be
#: recommended. Real names, so they match the board rather than piling up
#: unmatched warnings that have nothing to do with the test.
PICKED_IN_ORDER = (
    "Ja'Marr Chase",
    "Justin Jefferson",
    "Saquon Barkley",
    "Brock Bowers",
    "Josh Allen",
    "Puka Nacua",
    "Trey McBride",
)

#: What the fake Claude answers with. The advisor decides which pick the card is
#: *for*; this only decides who is in it.
LOOP_ADVICE = {
    "pick": "Bijan Robinson",
    "reason": "He plays every down and catches passes, which is worth a lot here.",
    "backups": [{"name": "Jahmyr Gibbs", "reason": "Scores nearly as often."}],
    "watch_out": "Check he is playing before you take him.",
}


def espn_picks(count: int) -> list[dict[str, Any]]:
    """``count`` picks as ESPN reports them, in a six-team snake."""
    order = [1, 2, 3, 4, 5, 6]
    picks = []
    for overall in range(1, count + 1):
        index = (overall - 1) % 6
        if ((overall - 1) // 6) % 2 == 1:
            index = 5 - index
        picks.append(
            {
                "overall_pick": overall,
                "round_num": (overall - 1) // 6 + 1,
                "round_pick": index + 1,
                "team_id": order[index],
                "player_id": 9000 + overall,
                "player_name": PICKED_IN_ORDER[overall - 1],
            }
        )
    return picks


async def advise_through_the_loop(db_path: Path, settings, picks_made: int):
    """Run one real tick against ``picks_made`` picks; return the loop and client.

    The board is seeded first because that is the state draft night starts in:
    the research job ran yesterday.
    """
    from draft_fixtures import FakeEspnClient, FakeRunner, RecordingBus, ok_result

    from hal_mary.draft.loop import DraftLoop

    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    client = FakeEspnClient(picks=espn_picks(picks_made))
    loop = DraftLoop(conn, settings, client, FakeRunner(settings, [ok_result(LOOP_ADVICE)]),
                     RecordingBus())
    await loop.run_once()
    conn.close()
    return loop, client


def let_the_picks_land(db_path: Path, client, count: int) -> None:
    """The first half of a tick: ESPN reports more picks, the board takes them.

    Deliberately the loop's own two calls in the loop's own order, and
    deliberately *without* the advisor — that is the window a tick passes
    through every turn, while the next card is still being written.
    """
    from hal_mary.draft.loop import apply_new_picks, pending_picks
    from hal_mary.espn.sync import sync_draft

    client.picks = espn_picks(count)
    conn = open_conn(db_path)
    sync_draft(conn, client)
    apply_new_picks(conn, pending_picks(conn), None)
    conn.close()


def text_of(html: str) -> str:
    """The page with its tags removed — what she actually reads."""
    return re.sub(r"<[^>]+>", " ", html)


# --- the page renders in every state -----------------------------------------


def test_draft_page_needs_a_password(db_path: Path, settings):
    with client_for(db_path, settings) as client:
        response = client.get("/draft")
    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login?next=/draft"


def test_root_goes_to_the_draft_page(db_path: Path, settings):
    """The one page she opens on draft night is the one the app lands on."""
    with signed_in(db_path, settings) as client:
        response = client.get("/")
    assert response.status_code == 302
    assert response.headers["location"] == "/draft"


def test_draft_page_renders_with_no_board_at_all(db_path: Path, settings):
    """An empty board is a named state, not an empty table.

    This is the box on draft morning if the research job never ran, and it is
    the one condition where every recommendation would be a guess.
    """
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        response = client.get("/draft")
    assert response.status_code == 200
    body = text_of(response.text).lower()
    assert "no researched list of players yet" in body
    assert "board_build" in response.text, "she needs the command that fixes it"


def test_draft_page_renders_with_a_board_and_no_advice(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        response = client.get("/draft")
    assert response.status_code == 200
    assert "Ja&#39;Marr Chase" in response.text or "Ja'Marr Chase" in response.text
    # Never an empty card: it says what it is waiting for.
    assert "no advice yet" in text_of(response.text).lower()


def test_draft_page_renders_on_a_completely_empty_database(db_path: Path, settings):
    """No league, no board, no picks. It must still be a page, not a 500."""
    with signed_in(db_path, settings) as client:
        response = client.get("/draft")
    assert response.status_code == 200
    assert "hal-mary" in response.text


# --- the advice card ---------------------------------------------------------


def test_the_advice_card_shows_the_pick_the_reason_and_the_backups(db_path: Path, settings):
    seed_league(db_path)
    seed_advice(db_path)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    readable = text_of(body)
    assert "Bijan Robinson" in readable
    assert "He plays every down" in readable
    assert "Saquon Barkley" in readable
    assert "Puka Nacua" in readable
    assert "check he is playing" in readable
    # Which pick it is for, and when it was written: a stale card must be
    # obviously stale rather than quietly wrong.
    assert "pick 6" in readable.lower()
    assert "just now" in readable.lower()


def test_a_fallback_card_is_visibly_different_from_a_researched_one(db_path: Path, settings):
    """``attempts == 0`` means no model call was made at all.

    She has to be able to tell a researched recommendation from a ranked-list
    guess at a glance, or she cannot weigh either.
    """
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    seed_advice(db_path, source="fallback", attempts=0)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "advice--fallback" in body, "a fallback card must be styled as one"
    readable = text_of(body).lower()
    assert "ranking list" in readable
    assert "researched" not in readable


def test_a_researched_card_says_so(db_path: Path, settings):
    seed_league(db_path)
    seed_advice(db_path, source="claude", attempts=1)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "advice--fallback" not in body
    assert "researched" in text_of(body).lower()


def test_advice_for_an_earlier_pick_is_not_presented_as_current(db_path: Path, settings):
    """Five picks have happened since this card was written."""
    seed_league(db_path)
    seed_advice(db_path, next_overall_pick=6)
    seed_picks(db_path, [(n, ((n - 1) % 6) + 1, f"Player {n}") for n in range(1, 9)])
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    readable = text_of(body).lower()
    assert "advice--stale" in body
    assert "was for pick 6" in readable


async def test_the_card_names_her_pick_not_the_pick_it_was_written_on(
    db_path: Path, settings
):
    """The card is written *before* her turn — that is the whole point of it.

    The loop advises as soon as she is within ``advise_within_picks``, so a card
    for pick 6 is normally written while pick 4 is on the clock. Labelling it
    with the pick it was written on makes a correct, current card announce that
    it is out of date and promise a replacement no code will ever write — on her
    turn, every turn, not as an edge case.
    """
    seed_league(db_path)
    _loop, client = await advise_through_the_loop(db_path, settings, picks_made=3)
    # Two more picks land, so hers is now on the clock — the card was for this.
    let_the_picks_land(db_path, client, count=5)

    with signed_in(db_path, settings) as client_:
        body = client_.get("/draft").text
    readable = text_of(body).lower()

    assert "take at pick 6" in readable, "the card must name the pick it is for"
    assert "pick 4" not in readable
    assert "advice--stale" not in body, "a card for the pick on the clock is current"
    assert "working out" not in readable, "nothing is being written; this is the card"
    assert "it is your pick" in readable


async def test_a_card_still_counts_while_her_turn_is_approaching(
    db_path: Path, settings
):
    """The ordinary case: the card exists, her pick is two away, all is well."""
    seed_league(db_path)
    await advise_through_the_loop(db_path, settings, picks_made=3)

    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    readable = text_of(body).lower()
    assert "take at pick 6" in readable
    assert "advice--stale" not in body
    assert "2 picks until yours" in readable


async def test_a_stale_card_being_replaced_says_both_things_in_one_line(
    db_path: Path, settings
):
    """Her pick has been made and the next one is also hers.

    This is the window every turn passes through: the board has taken the new
    picks and the next card has not been written yet. Two bands saying
    overlapping things would be two thirds of a phone screen spent before she
    reaches the recommendation, so they merge.
    """
    seed_league(db_path)
    _loop, client = await advise_through_the_loop(db_path, settings, picks_made=3)
    let_the_picks_land(db_path, client, count=6)

    with signed_in(db_path, settings) as client_:
        body = client_.get("/draft").text
    assert body.count("advice-band") == 1, "one band, not a stack of them"
    readable = text_of(body).lower()
    assert "working out pick 7" in readable
    assert "was for pick 6" in readable


def test_advice_being_worked_on_is_shown_as_a_state(db_path: Path, settings):
    """Her pick is imminent, something is polling, and no card exists yet."""
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    # Picks 1..5 made; pick 6 is hers and is on the clock, and no card exists
    # for it yet. The fresh sync is the loop's own evidence of life.
    seed_picks(db_path, [(n, n, f"Player {n}") for n in range(1, 6)])
    record_draft_sync(db_path, seconds_ago=1)
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "working out" in body


def test_nothing_promises_a_card_when_nothing_is_polling(db_path: Path, settings):
    """"Working out your pick" is an inference, and it needs a falsifier.

    The loop is allowed to be absent — ``start_draft_loop`` tolerates one that
    will not start — and with nothing polling, no card is coming. A band that
    promises one forever is worse than no band, because she waits.
    """
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    seed_picks(db_path, [(n, n, f"Player {n}") for n in range(1, 6)])
    # No sync_runs row at all: nothing has ever polled ESPN.
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "working out" not in body
    assert "no advice yet" in body


def test_a_long_dead_poller_stops_promising_a_card(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    seed_picks(db_path, [(n, n, f"Player {n}") for n in range(1, 6)])
    record_draft_sync(db_path, seconds_ago=600)
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "working out" not in body


def test_a_loop_that_is_not_running_says_so_on_the_page(db_path: Path, settings):
    """She must never be looking at frozen data with no sign anything is wrong."""
    from hal_mary.web.serve import start_draft_loop

    class RefusingThread:
        def __init__(self, settings_, bus) -> None:
            self.error = "EspnError: the ESPN cookies have expired"

        def start(self) -> bool:
            return False

        def stop(self) -> None:  # pragma: no cover - never reached
            pass

    seed_league(db_path)
    app = build_app(db_path, settings)
    start_draft_loop(app, settings, thread_factory=RefusingThread)

    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"password": PASSWORD})
        body = client.get("/draft").text
    readable = text_of(body).lower()
    assert "not following the draft" in readable
    assert "by hand" in readable, "tell her what still works"
    assert "espn cookies have expired" in readable, "and what to fix"


# --- turn status -------------------------------------------------------------


def test_turn_status_counts_the_picks_until_hers(db_path: Path, settings):
    seed_league(db_path)
    seed_picks(db_path, [(1, 1, "Player 1"), (2, 2, "Player 2")])
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "pick 3" in body
    # Team 3 picks 3rd, she is 6th: three teams go before her.
    assert "3 picks until yours" in body


def test_turn_status_says_when_it_is_her_pick(db_path: Path, settings):
    seed_league(db_path)
    seed_picks(db_path, [(n, n, f"Player {n}") for n in range(1, 6)])
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "your pick" in body


def test_the_turn_counter_does_not_render_after_the_last_pick(db_path: Path, settings):
    """``my_upcoming_picks`` empty is the only end-of-draft signal there is.

    Without the gate the page counts down forever past the last pick.
    """
    seed_league(db_path)
    seed_picks(db_path, [(n, ((n - 1) % 6) + 1, f"Player {n}") for n in range(1, 97)])
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "until yours" not in body
    assert "on the clock" not in body
    assert "draft is finished" in body


def test_the_page_says_the_draft_order_is_provisional_until_the_first_pick(
    db_path: Path, settings
):
    """The residual gap in Task 15, said out loud on the page.

    ESPN draws the order when the draft opens, and hal-mary can only read it off
    the board once a real pick has landed. In between, every number on this page
    comes from the pre-draft placeholder — and if ESPN drew Caroline first
    overall, the placeholder puts her next pick five away, past the advisor's
    window, so she gets no card at all for her opening pick while the page says
    four picks out. That is the worst possible moment to have nothing, and a
    person reading the page should not have to have read the runbook.
    """
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()

    assert "provisional" in body
    assert "sync" in body, "and what to do about it"


def test_that_note_goes_away_once_the_draft_is_running(db_path: Path, settings):
    """A caveat that never clears is a caveat nobody reads."""
    seed_league(db_path)
    seed_picks(db_path, [(1, 1, "Player 1")])
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()

    assert "provisional" not in body


def test_the_page_works_when_the_league_is_unknown(db_path: Path, settings):
    """No sync, no [league] config: there is no turn to report, but a page."""
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        response = client.get("/draft")
    assert response.status_code == 200
    assert "hal-mary sync" in response.text


# --- her roster --------------------------------------------------------------


def test_her_roster_shows_what_she_has_and_what_is_open(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    seed_picks(db_path, [(6, REAL_MY_TEAM_ID, "Bijan Robinson")])
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    readable = text_of(body)
    assert "Bijan Robinson" in readable
    # An open slot she has not filled has to be readable without counting, and
    # in words: "QB" is not a word she has any reason to know.
    assert "Quarterback" in readable
    assert "empty" in readable.lower()


def test_roster_headings_never_use_a_bare_position_code(db_path: Path, settings):
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    for word in ("Quarterback", "Running back", "Wide receiver", "Tight end", "Kicker"):
        assert word in body, f"{word} is missing; the roster is showing codes"


# --- the board ---------------------------------------------------------------


def test_the_board_lists_undrafted_players_with_what_she_needs_to_know(
    db_path: Path, settings
):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text)
    assert "Bijan Robinson" in body
    assert "ATL" in body
    assert "Bye week 5" in body
    assert "Runs and catches" in body


def test_drafted_players_are_visually_distinguished_from_available_ones(
    db_path: Path, settings
):
    seed_league(db_path)
    conn = open_conn(db_path)
    board = [dict(row) for row in SAMPLE_BOARD]
    board[0] = {**board[0], "drafted_by_team_id": 2, "drafted_at": "2026-09-07T00:01:00+00:00"}
    seed_board(conn, board)
    conn.close()
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "player--gone" in body, "a drafted player must be marked, not silently dropped"
    # And he is not offered as available.
    available = body.split("player--gone")[0]
    assert "Ja&#39;Marr Chase" not in available and "Ja'Marr Chase" not in available


def test_the_board_can_be_filtered_to_one_position_in_words(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        page = client.get("/draft").text
        filtered = text_of(client.get("/draft?position=RB").text)
    assert "Running backs" in page, "the filter must be labelled in words"
    assert "Bijan Robinson" in filtered
    assert "Ja'Marr Chase" not in filtered and "Ja&#39;Marr Chase" not in filtered


# --- unmatched picks ---------------------------------------------------------


def record_unmatched(db_path: Path, name: str, overall_pick: int = 4) -> int:
    conn = open_conn(db_path)
    cur = conn.execute(
        "INSERT INTO unmatched_picks (overall_pick, team_id, player_id, player_name,"
        " seen_at, noticed_at) VALUES (?, 2, NULL, ?, ?, ?)",
        (overall_pick, name, db.utc_now(), db.utc_now()),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return int(row_id)


def test_an_unknown_position_does_not_put_a_code_in_a_sentence(db_path: Path, settings):
    """The page's rule is no jargon; a fallback that prints the code breaks it."""
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        readable = text_of(client.get("/draft?position=ZZ").text)
    assert "ZZ" not in readable
    assert "that position" in readable.lower()


def test_unmatched_picks_appear_next_to_the_manual_entry_control(db_path: Path, settings):
    """The board and reality disagree about who is gone.

    That is the one condition that makes a recommendation actively *wrong*, and
    the fix is a human typing the name in — so it belongs beside that control,
    not only in a log.
    """
    seed_league(db_path)
    record_unmatched(db_path, "Someone Unknown")
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    readable = text_of(body)
    assert "Someone Unknown" in readable
    assert "could not find" in readable.lower()
    # Adjacent to the form, in the same card.
    card = body.split('id="manual"', 1)[1]
    assert "Someone Unknown" in card


def test_an_unmatched_warning_can_be_dismissed(db_path: Path, settings):
    seed_league(db_path)
    row_id = record_unmatched(db_path, "Someone Unknown")
    with signed_in(db_path, settings) as client:
        response = post(
            client,
            settings,
            "/draft/unmatched/resolve",
            {"unmatched_id": str(row_id)},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Someone Unknown" not in client.get("/draft").text

    conn = open_conn(db_path)
    resolved = conn.execute(
        "SELECT resolved_at FROM unmatched_picks WHERE id = ?", (row_id,)
    ).fetchone()["resolved_at"]
    conn.close()
    assert resolved is not None


# --- manual pick entry -------------------------------------------------------


def test_manual_pick_records_the_pick_and_returns_the_fragment(db_path: Path, settings):
    """The lifeline when ESPN stops updating. The whole no-ESPN contingency
    depends on this working."""
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()

    with signed_in(db_path, settings) as client:
        response = post(
            client,
            settings,
            "/draft/pick",
            {"player_name": "Bijan Robinson", "team_id": "2"},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    assert "Bijan Robinson" in response.text

    conn = open_conn(db_path)
    pick = conn.execute("SELECT * FROM draft_picks").fetchone()
    board_row = conn.execute(
        "SELECT * FROM board WHERE name = 'Bijan Robinson'"
    ).fetchone()
    conn.close()
    assert pick["player_name"] == "Bijan Robinson"
    assert pick["team_id"] == 2
    assert board_row["drafted_at"] is not None, "the board must agree he is gone"


def test_a_manual_pick_naming_nobody_on_the_board_says_so_plainly(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()

    with signed_in(db_path, settings) as client:
        response = post(
            client,
            settings,
            "/draft/pick",
            {"player_name": "Nobody At All", "team_id": ""},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    readable = text_of(response.text).lower()
    assert "could not find" in readable
    assert "nobody at all" in readable
    assert "spelling" in readable, "tell her what to do about it"


def test_a_manual_pick_with_no_board_does_not_claim_he_was_crossed_off(
    db_path: Path, settings
):
    """Draft morning, an unbuilt board and ESPN down is *the* lifeline case.

    ``apply_new_picks`` crosses nobody off when there is no board, and telling
    her "the next recommendation knows it" when no list exists is a false
    reassurance at exactly the moment she is relying on this control.
    """
    seed_league(db_path)  # league, but deliberately no board
    with signed_in(db_path, settings) as client:
        response = post(
            client,
            settings,
            "/draft/pick",
            {"player_name": "Bijan Robinson", "team_id": "2"},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    readable = text_of(response.text).lower()
    assert "the next recommendation knows it" not in readable
    assert "no researched list" in readable
    assert "recorded" in readable, "the pick is still kept"


def test_a_manual_pick_with_no_name_is_refused_kindly(db_path: Path, settings):
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        response = post(
            client,
            settings,
            "/draft/pick",
            {"player_name": "   ", "team_id": ""},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    assert "name" in text_of(response.text).lower()

    conn = open_conn(db_path)
    assert conn.execute("SELECT count(*) AS n FROM draft_picks").fetchone()["n"] == 0
    conn.close()


def test_manual_pick_without_htmx_redirects_back_to_the_page(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        response = post(
            client, settings, "/draft/pick", {"player_name": "Bijan Robinson", "team_id": ""}
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/draft"


def test_the_team_selector_lists_the_league(db_path: Path, settings):
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "Gridiron Gerbils" in body
    assert "Punt Intended" in body
    # And an honest "I do not know" option, because during a draft she often
    # will not.
    assert "not sure" in text_of(body).lower()


def test_a_manual_pick_publishes_so_other_screens_update(db_path: Path, settings):
    from draft_fixtures import RecordingBus

    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    bus = RecordingBus()
    with signed_in(db_path, settings, bus=bus) as client:
        post(client, settings, "/draft/pick", {"player_name": "Bijan Robinson", "team_id": ""})
    assert [event for event, _ in bus.published] == ["board_updated"]


# --- CSRF --------------------------------------------------------------------


def test_a_manual_pick_without_a_token_is_rejected(db_path: Path, settings):
    """The first genuinely state-changing POST in this app.

    Without this, any page open in any tab on the house network can post a pick
    into her draft.
    """
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        response = client.post("/draft/pick", data={"player_name": "Bijan Robinson"})
    assert response.status_code == 403

    conn = open_conn(db_path)
    assert conn.execute("SELECT count(*) AS n FROM draft_picks").fetchone()["n"] == 0
    conn.close()


def test_a_manual_pick_with_the_wrong_token_is_rejected(db_path: Path, settings):
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        response = client.post(
            "/draft/pick",
            data={"player_name": "Bijan Robinson", "csrf_token": "not-the-token"},
        )
    assert response.status_code == 403


def test_the_sync_button_carries_a_token_too(db_path: Path, settings):
    calls: list[int] = []
    with signed_in(db_path, settings, run_sync=lambda: calls.append(1) or {}) as client:
        refused = client.post("/sync")
        assert refused.status_code == 403
        assert calls == []
        allowed = post(client, settings, "/sync", {})
    assert allowed.status_code in (200, 303)
    assert calls == [1]


def test_a_refused_post_is_shown_rather_than_silently_dropped(db_path: Path, settings):
    """htmx does not swap an error response unless it is told to.

    Without this a token that had expired behind an open page would make the
    "mark him taken" button do nothing at all, visibly — which during a draft is
    the worst way for anything to fail.
    """
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        refused = client.post(
            "/draft/pick",
            data={"player_name": "Bijan Robinson"},
            headers={"HX-Request": "true"},
        )
        body = client.get("/draft").text
    assert refused.status_code == 403
    assert "nothing was changed" in refused.text
    assert "shouldSwap" in body, "the page must render a refusal, not swallow it"


def test_the_token_is_rendered_into_every_form_on_the_draft_page(db_path: Path, settings):
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
        token = csrf_of(client, settings)
    forms = re.findall(r"<form.*?</form>", body, flags=re.DOTALL)
    assert forms, "the draft page has no forms at all"
    for form in forms:
        assert token in form, "a form on the draft page cannot be submitted"


def test_the_token_cookie_is_not_readable_by_script(db_path: Path, settings):
    """The token is rendered server-side, so the cookie never needs to be read
    by JavaScript — and a cookie script cannot read is one XSS cannot steal."""
    with client_for(db_path, settings) as client:
        response = client.get("/login")
    header = response.headers["set-cookie"]
    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()


def test_the_token_survives_across_requests(db_path: Path, settings):
    with signed_in(db_path, settings) as client:
        first = csrf_of(client, settings)
        client.get("/draft")
        assert csrf_of(client, settings) == first


# --- staleness ---------------------------------------------------------------


def record_draft_sync(db_path: Path, seconds_ago: float) -> None:
    from datetime import UTC, datetime, timedelta

    when = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    conn = open_conn(db_path)
    conn.execute(
        "INSERT INTO sync_runs (kind, started_at, finished_at, status) VALUES"
        " ('draft', ?, ?, 'ok')",
        (when, when),
    )
    conn.commit()
    conn.close()


def test_the_stale_banner_appears_once_the_draft_sync_falls_behind(db_path: Path, settings):
    seed_league(db_path)
    seed_picks(db_path, [(1, 1, "Player 1")])
    record_draft_sync(db_path, seconds_ago=95)
    with signed_in(db_path, settings) as client:
        body = text_of(client.get("/draft").text).lower()
    assert "last heard from espn" in body
    # The actual age, not a vague warning. Matched with a little slack because
    # a timestamp truncated to whole seconds plus the time this test takes can
    # land a second either side of ninety-five.
    shown = re.search(r"(\d+) seconds ago", body)
    assert shown is not None, "the banner must say how far behind it is"
    assert 94 <= int(shown.group(1)) <= 100


def test_the_stale_banner_stays_away_when_the_sync_is_fresh(db_path: Path, settings):
    seed_league(db_path)
    seed_picks(db_path, [(1, 1, "Player 1")])
    record_draft_sync(db_path, seconds_ago=3)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "stale-banner" not in body


def test_no_stale_banner_before_the_draft_has_started(db_path: Path, settings):
    """Nothing has been picked yet, so there is nothing to be behind on."""
    seed_league(db_path)
    record_draft_sync(db_path, seconds_ago=600)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "stale-banner" not in body


def test_no_stale_banner_once_the_draft_is_over(db_path: Path, settings):
    seed_league(db_path)
    seed_picks(db_path, [(n, ((n - 1) % 6) + 1, f"Player {n}") for n in range(1, 97)])
    record_draft_sync(db_path, seconds_ago=6000)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "stale-banner" not in body


# --- live updates ------------------------------------------------------------


def test_the_live_fragment_renders_on_its_own(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    seed_advice(db_path)
    with signed_in(db_path, settings) as client:
        response = client.get("/draft/live")
    assert response.status_code == 200
    assert "<html" not in response.text, "a fragment, not a page: a reload loses her scroll"
    assert "Bijan Robinson" in response.text


def test_the_live_fragment_keeps_the_position_filter(db_path: Path, settings):
    """A pick landing must not throw her back to the whole board.

    She filters to running backs, somebody picks, the fragment swaps — and if
    the swap drops the filter she is looking at a different list than the one
    she chose, mid-turn.
    """
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        filtered = text_of(client.get("/draft/live?position=RB").text)
        page = client.get("/draft?position=RB").text
    assert "Bijan Robinson" in filtered
    assert "Ja'Marr Chase" not in filtered and "Ja&#39;Marr Chase" not in filtered
    # And the page hands its own query string to the refresh, so the swap asks
    # for the same list.
    assert "location.search" in page


def test_a_filter_for_a_position_nobody_has_is_not_offered(db_path: Path, settings):
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        body = client.get("/draft?position=ZZ").text
    assert "position=ZZ" not in body, "a made-up position must not become a button"


def test_a_filter_survives_its_players_all_being_taken(db_path: Path, settings):
    """Otherwise the button she is standing on vanishes under her."""
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(
        conn,
        [
            {**row, "drafted_by_team_id": 2, "drafted_at": "2026-09-07T00:01:00+00:00"}
            if row["position"] == "TE"
            else dict(row)
            for row in SAMPLE_BOARD
        ],
    )
    conn.close()
    with signed_in(db_path, settings) as client:
        body = client.get("/draft?position=TE").text
    assert "Tight ends" in body, "the filter she is standing on must survive"
    assert "every tight end on the list has been taken" in text_of(body).lower()


def test_the_live_fragment_needs_a_password(db_path: Path, settings):
    with client_for(db_path, settings) as client:
        response = client.get("/draft/live")
    assert response.status_code in (302, 303, 307)


@pytest.mark.parametrize("event", ["advice", "board_updated", "board_missing"])
def test_the_page_swaps_a_fragment_for_each_event_type(db_path: Path, settings, event: str):
    """The listener must handle all three, and swap rather than reload."""
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert f'"{event}"' in body, f"nothing on the page listens for {event}"
    assert "location.reload" not in body, "a reload loses her scroll position mid-draft"
    assert "/draft/live" in body


def test_the_page_falls_back_to_polling_and_says_it_is_reconnecting(
    db_path: Path, settings
):
    seed_league(db_path)
    with signed_in(db_path, settings) as client:
        body = client.get("/draft").text
    assert "reconnecting" in body.lower()
    assert "10000" in body, "the poll interval must be ten seconds"


async def test_events_carries_each_draft_event_to_a_listening_page(db_path: Path, settings):
    """End to end at the ASGI layer: what the browser's EventSource sees."""
    from test_web import EventProbe

    from hal_mary.events import EventBus

    bus = EventBus()
    app = build_app(db_path, settings, bus=bus)
    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"password": PASSWORD})
        name = settings.web.session_cookie
        cookie = f"{name}={client.cookies[name]}"

    async with EventProbe(app, cookie) as probe:
        assert (await probe.frame()).startswith(":")
        for event in ("board_updated", "advice", "board_missing"):
            bus.publish(event, {"next_overall_pick": 7})
            assert f"event: {event}" in await probe.frame()


# --- the loop beside the web app ---------------------------------------------


def test_the_page_renders_with_no_draft_loop_running(db_path: Path, settings):
    """The loop is a separate thread. The page is not allowed to depend on it."""
    seed_league(db_path)
    conn = open_conn(db_path)
    seed_board(conn, SAMPLE_BOARD)
    conn.close()
    with signed_in(db_path, settings) as client:
        assert client.get("/draft").status_code == 200


def test_a_loop_that_cannot_start_does_not_take_down_the_web_app(
    db_path: Path, settings, caplog
):
    from hal_mary.draft.runner import DraftLoopThread

    def explode(*_args: Any, **_kwargs: Any):
        raise RuntimeError("no ESPN credentials on this box")

    thread = DraftLoopThread(settings, bus=None, build_loop=explode)
    assert thread.start() is False
    assert thread.error is not None
    assert "no ESPN credentials" in thread.error

    with signed_in(db_path, settings) as client:
        assert client.get("/draft").status_code == 200


def test_the_loop_thread_runs_the_loop_and_stops_cleanly(db_path: Path, settings):
    import threading

    from hal_mary.draft.runner import DraftLoopThread

    ticked = threading.Event()

    class SpyLoop:
        def __init__(self) -> None:
            self.stopped = False
            self._done = False

        async def run_forever(self) -> None:
            ticked.set()
            while not self.stopped:
                await _sleep_a_moment()

        def stop(self) -> None:
            self.stopped = True

    async def _sleep_a_moment() -> None:
        import asyncio

        await asyncio.sleep(0.01)

    spy = SpyLoop()
    thread = DraftLoopThread(settings, bus=None, build_loop=lambda conn, bus: spy)
    assert thread.start() is True
    assert ticked.wait(timeout=5), "the loop never ran"
    thread.stop(timeout=5)
    assert spy.stopped is True
    assert thread.alive is False


def test_the_loop_thread_opens_its_own_connection(db_path: Path, settings):
    """6b's connection cannot cross threads; the thread has to make its own."""
    import threading

    from hal_mary.draft.runner import DraftLoopThread

    seen: dict[str, Any] = {}
    built = threading.Event()

    class IdleLoop:
        def __init__(self) -> None:
            self.running = True

        async def run_forever(self) -> None:
            import asyncio

            built.set()
            while self.running:
                await asyncio.sleep(0.01)

        def stop(self) -> None:
            self.running = False

    def build(conn: sqlite3.Connection, bus: Any):
        seen["conn"] = conn
        seen["thread"] = threading.current_thread().name
        return IdleLoop()

    thread = DraftLoopThread(settings, bus=None, build_loop=build)
    thread.start()
    assert built.wait(timeout=5)
    thread.stop(timeout=2)
    assert isinstance(seen["conn"], sqlite3.Connection)
    assert seen["thread"] != threading.main_thread().name


def test_the_draft_loop_is_started_beside_the_web_app(db_path: Path, settings):
    """One process, two loops. They share the event bus and nothing else."""
    from hal_mary.web.serve import start_draft_loop

    app = build_app(db_path, settings)
    made: dict[str, Any] = {}

    class FakeThread:
        def __init__(self, settings_, bus) -> None:
            made["bus"] = bus
            self.error = None

        def start(self) -> bool:
            made["started"] = True
            return True

        def stop(self) -> None:
            made["stopped"] = True

    thread = start_draft_loop(app, settings, thread_factory=FakeThread)
    assert made["started"] is True
    assert made["bus"] is app.state.bus, "the loop must publish onto the page's own bus"
    assert app.state.draft_loop is thread


def test_a_draft_loop_that_will_not_start_leaves_the_web_app_serving(
    db_path: Path, settings, caplog
):
    from hal_mary.web.serve import start_draft_loop

    class RefusingThread:
        def __init__(self, settings_, bus) -> None:
            self.error = "EspnError: no cookies"

        def start(self) -> bool:
            return False

        def stop(self) -> None:  # pragma: no cover - never reached
            pass

    app = build_app(db_path, settings)
    with caplog.at_level("WARNING"):
        start_draft_loop(app, settings, thread_factory=RefusingThread)
    assert "no cookies" in caplog.text

    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"password": PASSWORD})
        assert client.get("/draft").status_code == 200
