"""Tests for the ESPN read client.

The client's whole job is to turn ESPN's JSON into plain dicts, so almost every
test here is "feed it a fixture, assert the dict". The exceptions are the error
mapping tests and the one that matters most: ``draft_picks()`` must ignore
``draftDetail.drafted``, because the espn_api library does not, and a draft that
ESPN has not flagged as complete is exactly the draft we need to read.
"""

from __future__ import annotations

import json

import httpx
import pytest
from espn_api.requests.espn_requests import (
    ESPNAccessDenied,
    ESPNInvalidLeague,
    ESPNUnknownError,
)

from conftest import (
    FIXTURE_ENV,
    FIXTURE_LEAGUE_ID,
    FIXTURE_SEASON,
    draft_transport,
    load_espn_fixture,
)
from hal_mary.config import EspnConfig, load_settings
from hal_mary.espn import client as client_module
from hal_mary.espn.client import (
    DRAFT_VIEW,
    EspnAuthError,
    EspnClient,
    EspnLeagueNotFound,
    EspnUnavailable,
)


@pytest.fixture
def settings():
    return load_settings(env=FIXTURE_ENV)


@pytest.fixture
def anonymous_settings():
    """Settings from a box that has never seen a .env — every secret unset."""
    return load_settings(env={})


def client_for(settings, **kwargs) -> EspnClient:
    return EspnClient(settings, **kwargs)


# --- construction ----------------------------------------------------------


def test_constructing_without_cookies_does_not_raise(anonymous_settings, no_network):
    """`hal-mary --help` and the test suite must not depend on ESPN being up."""
    client = client_for(anonymous_settings)
    assert client.settings.espn_s2 is None


def test_construction_touches_no_network(settings, no_network):
    client_for(settings)


def test_the_http_timeouts_come_from_config(settings):
    """Rule 5: timeouts live in config.toml, not in the code that uses them."""
    tuned = settings.model_copy(
        update={"espn": EspnConfig(connect_timeout_s=1.5, read_timeout_s=2.5)}
    )

    timeout = client_for(tuned).timeout

    assert timeout.connect == 1.5
    assert timeout.read == 2.5


def test_the_shipped_config_bounds_the_draft_poll(settings):
    """The read timeout is only sane relative to the pick clock next door."""
    assert settings.espn.read_timeout_s > settings.draft.poll_seconds


# --- league_settings -------------------------------------------------------


def test_league_settings_maps_the_fixture(settings, fake_espn):
    result = client_for(settings).league_settings()

    assert result["season"] == FIXTURE_SEASON
    assert result["league_id"] == FIXTURE_LEAGUE_ID
    assert result["name"] == "The Gridiron Gauntlet"
    assert result["team_count"] == 4
    assert result["scoring_type"] == "H2H_POINTS"
    assert result["draft_type"] == "SNAKE"
    assert result["draft_date"] == "2025-08-26T00:00:00+00:00"


def test_league_settings_roster_slots_are_named_and_drop_empty_slots(settings, fake_espn):
    slots = client_for(settings).league_settings()["roster_slots"]

    assert slots == {
        "QB": 1,
        "RB": 2,
        "WR": 2,
        "TE": 1,
        "D/ST": 1,
        "K": 1,
        "BE": 6,
        "IR": 1,
        "RB/WR/TE": 1,
    }


def test_league_settings_carries_the_raw_settings_json(settings, fake_espn):
    raw = json.loads(client_for(settings).league_settings()["raw_json"])

    # Anything we did not think to extract is still reachable.
    assert raw["acquisitionSettings"]["acquisitionBudget"] == 100
    assert raw["tradeSettings"]["deadlineDate"] == 1763424000000


def test_league_settings_returns_plain_data(settings, fake_espn):
    result = client_for(settings).league_settings()
    # A round trip through JSON proves nothing library-shaped leaked out.
    assert json.loads(json.dumps(result)) == result


# --- teams -----------------------------------------------------------------


def test_teams_maps_the_fixture(settings, fake_espn):
    fake_espn.league_fixture = "teams.json"

    assert client_for(settings).teams() == [
        {
            "team_id": 1,
            "name": "Hail Mary",
            "owner": "Caroline Reed",
            "abbrev": "HAL",
            "draft_slot": 2,
        },
        {
            "team_id": 2,
            "name": "Blitz Brigade",
            "owner": "Dana Whitlock",
            "abbrev": "BLZ",
            "draft_slot": 4,
        },
        {
            "team_id": 3,
            "name": "Play Action Heroes",
            "owner": "Marcus Ellis",
            "abbrev": "PLY",
            "draft_slot": 1,
        },
        {
            "team_id": 4,
            "name": "Touchdown Zone",
            "owner": "Priya Raman",
            "abbrev": "TDZ",
            "draft_slot": 3,
        },
    ]


# --- rosters ---------------------------------------------------------------


def test_rosters_maps_the_fixture(settings, fake_espn):
    rows = client_for(settings).rosters()

    assert len(rows) == 6
    assert rows[0] == {
        "team_id": 1,
        "player_id": 3139477,
        "name": "Patrick Mahomes",
        "position": "QB",
        "pro_team": "KC",
        "injury_status": "ACTIVE",
        "slot": "QB",
    }
    kelce = next(row for row in rows if row["player_id"] == 3116365)
    assert kelce["slot"] == "BE"
    bijan = next(row for row in rows if row["player_id"] == 4362628)
    assert bijan["injury_status"] == "QUESTIONABLE"


def test_rosters_is_empty_when_no_team_has_a_roster(settings, fake_espn):
    fake_espn.league_fixture = "teams.json"
    assert client_for(settings).rosters() == []


# --- free agents -----------------------------------------------------------


def test_free_agents_maps_the_fixture(settings, fake_espn):
    rows = client_for(settings).free_agents()

    assert rows[0] == {
        "player_id": 4426515,
        "name": "Sam LaPorta",
        "position": "TE",
        "pro_team": "DET",
        "injury_status": "OUT",
        "percent_owned": 62.3,
    }
    assert [row["name"] for row in rows] == ["Sam LaPorta", "Rome Odunze", "Justin Tucker"]


# --- draft picks: the raw-endpoint path ------------------------------------


def test_draft_picks_ignores_the_drafted_flag(settings, fake_espn):
    """The whole reason draft picks bypass espn_api.

    ``draft_detail_partial.json`` has ``drafted: false`` and three picks. The
    library's ``_fetch_draft`` returns nothing for that payload; we must return
    three picks.
    """
    payload = load_espn_fixture("draft_detail_partial.json")
    assert payload["draftDetail"]["drafted"] is False

    client = client_for(settings, transport=draft_transport(payload))
    picks = client.draft_picks()

    assert len(picks) == 3


def test_draft_picks_map_the_documented_shape(settings, fake_espn):
    payload = load_espn_fixture("draft_detail_partial.json")
    client = client_for(settings, transport=draft_transport(payload))

    assert client.draft_picks()[0] == {
        "overall_pick": 1,
        "round_num": 1,
        "round_pick": 1,
        "team_id": 3,
        "player_id": 4362628,
        "player_name": "Bijan Robinson",
    }


def test_draft_picks_are_sorted_by_overall_pick(settings, fake_espn):
    payload = load_espn_fixture("draft_detail_full.json")
    on_the_wire = [p["overallPickNumber"] for p in payload["draftDetail"]["picks"]]
    assert on_the_wire != sorted(on_the_wire), "fixture must be out of order to prove the sort"

    client = client_for(settings, transport=draft_transport(payload))

    assert [p["overall_pick"] for p in client.draft_picks()] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_draft_picks_is_empty_before_the_draft_starts(settings, fake_espn):
    payload = load_espn_fixture("draft_detail_empty.json")
    client = client_for(settings, transport=draft_transport(payload))

    assert client.draft_picks() == []


def test_an_unknown_player_id_yields_a_null_name_rather_than_raising(settings, fake_espn):
    payload = load_espn_fixture("draft_detail_full.json")
    client = client_for(settings, transport=draft_transport(payload))

    last = client.draft_picks()[-1]
    assert last["player_id"] == 9999999
    assert last["player_name"] is None


def test_draft_picks_survive_a_broken_name_lookup(settings, fake_espn):
    """Names are a nicety; picks are not. A dead library call must not lose picks."""
    fake_espn.status = 500
    payload = load_espn_fixture("draft_detail_partial.json")
    client = client_for(settings, transport=draft_transport(payload))

    assert [p["player_name"] for p in client.draft_picks()] == [None, None, None]


def test_draft_picks_calls_the_documented_endpoint_with_cookies(settings, fake_espn):
    seen: list[httpx.Request] = []
    payload = load_espn_fixture("draft_detail_partial.json")
    client = client_for(settings, transport=draft_transport(payload, requests_seen=seen))

    client.draft_picks()

    request = seen[0]
    assert str(request.url).startswith(
        "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
        f"{FIXTURE_SEASON}/segments/0/leagues/{FIXTURE_LEAGUE_ID}"
    )
    assert request.url.params["view"] == DRAFT_VIEW
    cookie = request.headers["cookie"]
    assert "espn_s2=fake-espn-s2-cookie" in cookie
    assert "SWID=" in cookie


def test_draft_picks_are_fetched_fresh_every_call(settings, fake_espn):
    """The draft loop polls this; a cached answer would never see a new pick."""
    seen: list[httpx.Request] = []
    payload = load_espn_fixture("draft_detail_partial.json")
    client = client_for(settings, transport=draft_transport(payload, requests_seen=seen))

    client.draft_picks()
    client.draft_picks()

    assert len(seen) == 2


# --- the pre-populated board: a pick is not a pick without a player ---------
#
# ESPN writes every slot of the draft board before the draft starts. The real
# 2026 league answered the very first sync with 96 rows — 6 teams by 16 rounds —
# every one carrying ``playerId: -1``. ``draft_detail_prepopulated_real_league.json``
# is that payload's shape.

PREPOPULATED = "draft_detail_prepopulated_real_league.json"

#: Players the fake ESPN can name, for the slots a test fills in.
MADE_PLAYERS = (4362628, 4241389, 3139477)


def _board_with_picks_made(count: int = 3, *, reverse: bool = True) -> dict:
    """The pre-populated board with the first ``count`` slots given real players.

    The picks list is reversed by default so that a test asserting overall order
    is asserting the sort, not the order ESPN happened to send.
    """
    payload = load_espn_fixture(PREPOPULATED)
    picks = payload["draftDetail"]["picks"]
    for index in range(count):
        picks[index]["playerId"] = MADE_PLAYERS[index % len(MADE_PLAYERS)]
    if reverse:
        picks.reverse()
    return payload


def test_the_prepopulated_board_reads_back_as_ninety_six_empty_slots():
    """Pins the fixture against the payload it was built from."""
    picks = load_espn_fixture(PREPOPULATED)["draftDetail"]["picks"]

    assert len(picks) == 96
    assert {pick["playerId"] for pick in picks} == {-1}
    assert {pick["roundId"] for pick in picks} == set(range(1, 17))
    assert {pick["teamId"] for pick in picks} == {1, 2, 3, 4, 5, 6}


def test_a_prepopulated_board_is_no_picks_at_all(settings, fake_espn):
    """The bug: 96 placeholder rows were read as 96 completed picks."""
    payload = load_espn_fixture(PREPOPULATED)
    client = client_for(settings, transport=draft_transport(payload))

    assert client.draft_picks() == []


def test_only_the_slots_with_a_real_player_count_as_picks(settings, fake_espn):
    payload = _board_with_picks_made(3)
    client = client_for(settings, transport=draft_transport(payload))

    picks = client.draft_picks()

    assert [pick["overall_pick"] for pick in picks] == [1, 2, 3]
    assert [pick["player_id"] for pick in picks] == list(MADE_PLAYERS)


@pytest.mark.parametrize(
    ("player_id", "why"),
    [
        (-1, "what ESPN really writes into an unmade slot"),
        (0, "the other falsy id ESPN could plausibly use"),
        (None, "an explicit null"),
        ("missing", "no playerId key at all"),
    ],
)
def test_a_slot_without_a_real_player_id_is_not_a_pick(settings, fake_espn, player_id, why):
    payload = load_espn_fixture("draft_detail_partial.json")
    for raw in payload["draftDetail"]["picks"]:
        if player_id == "missing":
            del raw["playerId"]
        else:
            raw["playerId"] = player_id
    client = client_for(settings, transport=draft_transport(payload))

    assert client.draft_picks() == [], why


def test_a_board_with_no_made_picks_does_not_build_the_player_name_map(settings, fake_espn):
    """Naming nobody costs a whole league fetch; the draft loop polls this."""
    payload = load_espn_fixture(PREPOPULATED)
    client = client_for(settings, transport=draft_transport(payload))

    client.draft_picks()

    assert fake_espn.calls == []


# --- the schedule the placeholders carry -----------------------------------


def test_draft_schedule_returns_every_slot_of_the_prepopulated_board(settings, fake_espn):
    payload = load_espn_fixture(PREPOPULATED)
    client = client_for(settings, transport=draft_transport(payload))

    schedule = client.draft_schedule()

    assert len(schedule) == 96
    assert [slot["overall_pick"] for slot in schedule] == list(range(1, 97))
    assert not any(slot["made"] for slot in schedule)


def test_draft_schedule_maps_the_documented_shape(settings, fake_espn):
    payload = load_espn_fixture(PREPOPULATED)
    client = client_for(settings, transport=draft_transport(payload))

    schedule = client.draft_schedule()

    assert schedule[0] == {
        "overall_pick": 1,
        "round_num": 1,
        "round_pick": 1,
        "team_id": 1,
        "made": False,
    }
    assert schedule[-1] == {
        "overall_pick": 96,
        "round_num": 16,
        "round_pick": 6,
        "team_id": 1,
        "made": False,
    }


def test_draft_schedule_owns_each_slot_to_the_right_team(settings, fake_espn):
    """A six-team snake off pickOrder [1..6]: down, then back up."""
    payload = load_espn_fixture(PREPOPULATED)
    client = client_for(settings, transport=draft_transport(payload))

    owners = [slot["team_id"] for slot in client.draft_schedule()]

    assert owners[:12] == [1, 2, 3, 4, 5, 6, 6, 5, 4, 3, 2, 1]
    assert owners[90:] == [6, 5, 4, 3, 2, 1]


def test_draft_schedule_marks_the_slots_that_have_been_filled(settings, fake_espn):
    payload = _board_with_picks_made(3)
    client = client_for(settings, transport=draft_transport(payload))

    schedule = client.draft_schedule()

    assert [slot["overall_pick"] for slot in schedule if slot["made"]] == [1, 2, 3]
    assert len(schedule) == 96


def test_draft_schedule_is_empty_when_espn_has_no_board_yet(settings, fake_espn):
    payload = load_espn_fixture("draft_detail_empty.json")
    client = client_for(settings, transport=draft_transport(payload))

    assert client.draft_schedule() == []


# --- error mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, EspnAuthError),
        (403, EspnAuthError),
        (404, EspnLeagueNotFound),
        (500, EspnUnavailable),
        (503, EspnUnavailable),
    ],
)
def test_draft_picks_maps_http_status_to_a_typed_error(settings, status, expected):
    client = client_for(settings, transport=draft_transport(status=status))

    with pytest.raises(expected):
        client.draft_picks()


def test_a_hung_request_becomes_espn_unavailable(settings):
    transport = draft_transport(raises=httpx.ReadTimeout("too slow"))
    client = client_for(settings, transport=transport)

    with pytest.raises(EspnUnavailable):
        client.draft_picks()


def test_draft_picks_without_cookies_is_an_auth_error(anonymous_settings, no_network):
    client = client_for(anonymous_settings, transport=draft_transport({}))

    with pytest.raises(EspnAuthError):
        client.draft_picks()


@pytest.mark.parametrize(
    ("library_error", "expected"),
    [
        (ESPNAccessDenied("nope"), EspnAuthError),
        (ESPNInvalidLeague("nope"), EspnLeagueNotFound),
        (ESPNUnknownError("nope"), EspnUnavailable),
    ],
)
def test_library_exceptions_are_wrapped(settings, monkeypatch, library_error, expected):
    def explode(*args, **kwargs):
        raise library_error

    monkeypatch.setattr(client_module, "_build_league", explode)

    with pytest.raises(expected):
        client_for(settings).teams()


# --- check_auth ------------------------------------------------------------


def test_check_auth_reports_success(settings):
    ok, reason = client_for(settings, transport=draft_transport({})).check_auth()

    assert ok is True
    assert reason


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "expired"),
        (404, "not found"),
        (503, "unavailable"),
    ],
)
def test_check_auth_distinguishes_the_failures_a_human_must_react_to(settings, status, needle):
    client = client_for(settings, transport=draft_transport(status=status))

    ok, reason = client.check_auth()

    assert ok is False
    assert needle in reason.lower()


def test_check_auth_without_cookies_says_so_without_a_request(anonymous_settings, no_network):
    seen: list[httpx.Request] = []
    client = client_for(anonymous_settings, transport=draft_transport({}, requests_seen=seen))

    ok, reason = client.check_auth()

    assert ok is False
    assert "ESPN_S2" in reason
    assert seen == []


def test_check_auth_reports_a_hung_request(settings):
    transport = draft_transport(raises=httpx.ConnectError("no route"))
    ok, reason = client_for(settings, transport=transport).check_auth()

    assert ok is False
    assert "unreachable" in reason.lower()


def test_check_auth_without_a_league_id_says_so_without_a_request(no_network):
    """Cookies alone are not enough: without LEAGUE_ID there is no URL to call."""
    seen: list[httpx.Request] = []
    settings = load_settings(env={"ESPN_S2": "cookie", "SWID": "{swid}"})
    client = client_for(settings, transport=draft_transport({}, requests_seen=seen))

    ok, reason = client.check_auth()

    assert ok is False
    assert "LEAGUE_ID" in reason
    assert seen == []


# --- player_name_map -------------------------------------------------------


def test_player_name_map_labels_players_from_the_pro_player_list(settings, fake_espn):
    names = client_for(settings).player_name_map()

    assert names[3139477] == "Patrick Mahomes"
    assert names[4426515] == "Sam LaPorta"
    assert 9999999 not in names
    assert all(isinstance(key, int) for key in names)


def test_a_failed_name_lookup_is_retried_rather_than_cached(settings, fake_espn, monkeypatch):
    """The finding that would have bitten on draft night.

    The draft loop holds one client for the whole draft. If a transient ESPN 500
    while fetching the player list cached an empty map, every pick for the rest
    of the evening would render as "player 12345" — the failure outliving its
    cause by three hours. With the retry cooldown elapsed, the same client
    recovers real names.
    """
    monkeypatch.setattr(client_module, "NAME_MAP_RETRY_COOLDOWN_S", 0.0)
    payload = load_espn_fixture("draft_detail_partial.json")
    client = client_for(settings, transport=draft_transport(payload))

    fake_espn.status = 500
    assert [pick["player_name"] for pick in client.draft_picks()] == [None, None, None]

    fake_espn.status = 200
    assert client.draft_picks()[0]["player_name"] == "Bijan Robinson"


def test_a_failed_name_lookup_is_not_retried_on_every_poll(settings, fake_espn):
    """...but it is not retried on every five-second tick either.

    Rebuilding the map is a full league fetch. Hammering an ESPN that is already
    failing, once every five seconds for an evening, is how a transient outage
    becomes a blocked cookie.
    """
    payload = load_espn_fixture("draft_detail_partial.json")
    client = client_for(settings, transport=draft_transport(payload))
    fake_espn.status = 500

    client.draft_picks()
    after_first = len(fake_espn.calls)
    client.draft_picks()

    assert len(fake_espn.calls) == after_first


def test_player_name_map_is_built_once_and_reused(settings, fake_espn):
    client = client_for(settings)

    client.player_name_map()
    calls_after_first = len(fake_espn.calls)
    client.player_name_map()

    assert len(fake_espn.calls) == calls_after_first


# --- how far along is the draft? -------------------------------------------
#
# The draft loop chooses its cadence from this. It has to come off the read the
# loop already makes: a second GET on the pick clock is exactly the over-polling
# the phases exist to remove.


def test_draft_status_is_none_before_anything_has_been_read(settings, fake_espn):
    """"Nothing has been read" and "no draft" are different answers."""
    client = client_for(settings, transport=draft_transport(load_espn_fixture(PREPOPULATED)))

    assert client.draft_status() is None


def test_draft_status_costs_no_extra_request(settings, fake_espn):
    seen: list[httpx.Request] = []
    client = client_for(
        settings, transport=draft_transport(load_espn_fixture(PREPOPULATED), requests_seen=seen)
    )

    client.draft_picks()
    client.draft_status()
    client.draft_status()

    assert len(seen) == 1, "the status comes off the read draft_picks already made"


def test_the_real_pre_draft_payload_reads_as_ninety_six_slots_and_no_picks(settings, fake_espn):
    """What the live league answers today, field for field."""
    client = client_for(settings, transport=draft_transport(load_espn_fixture(PREPOPULATED)))

    client.draft_picks()
    status = client.draft_status()

    assert status["slots"] == 96
    assert status["picks_made"] == 0
    assert status["drafted"] is False


def test_draft_status_counts_only_the_slots_with_a_real_player(settings, fake_espn):
    client = client_for(settings, transport=draft_transport(_board_with_picks_made(3)))

    client.draft_picks()

    assert client.draft_status()["picks_made"] == 3


def test_espns_own_flags_are_carried_through_as_they_are(settings, fake_espn):
    """Carried, not obeyed — the loop decides. A missing flag stays None."""
    payload = _board_with_picks_made(3)
    payload["draftDetail"]["inProgress"] = True
    payload["draftDetail"]["drafted"] = True
    client = client_for(settings, transport=draft_transport(payload))

    client.draft_picks()
    assert client.draft_status()["in_progress"] is True
    assert client.draft_status()["drafted"] is True

    payload["draftDetail"].pop("inProgress")
    other = client_for(settings, transport=draft_transport(payload))
    other.draft_picks()
    assert other.draft_status()["in_progress"] is None, "absent is not False"


# --- current_week ----------------------------------------------------------


def test_current_week_reads_the_scoring_period(settings, fake_espn):
    """Nothing else in the schema knows which NFL week it is.

    The bye-week action producer asks "is this player on bye *right now*", and
    every in-season job asks "what week is it". The only honest answer comes
    from ESPN rather than from arithmetic over a calendar the code would have to
    hardcode; a wrong answer flags the wrong players — or nobody.
    """
    assert client_for(settings).current_week() == 1


def test_current_week_without_cookies_raises_rather_than_guessing(anonymous_settings, no_network):
    """No cookies at all is a configuration problem, not a week ESPN withheld."""
    with pytest.raises(EspnAuthError):
        client_for(anonymous_settings).current_week()


def test_current_week_is_none_rather_than_a_guess_when_espn_will_not_say(settings, fake_espn):
    """No week beats a guessed one. A calendar-derived week would silently move
    the bye check onto the wrong players, which is worse than not making it."""
    fake_espn.status = 401
    assert client_for(settings).current_week() is None
