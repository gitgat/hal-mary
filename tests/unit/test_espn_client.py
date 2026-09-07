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
from hal_mary.config import load_settings
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
    from hal_mary.espn import client as client_module

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


def test_player_name_map_is_built_once_and_reused(settings, fake_espn):
    client = client_for(settings)

    client.player_name_map()
    calls_after_first = len(fake_espn.calls)
    client.player_name_map()

    assert len(fake_espn.calls) == calls_after_first
