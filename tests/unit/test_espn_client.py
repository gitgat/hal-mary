"""Tests for the ESPN read client.

The client's whole job is to turn ESPN's JSON into plain dicts, so almost every
test here is "feed it a fixture, assert the dict". The exceptions are the error
mapping tests and the one that matters most: ``draft_picks()`` must ignore
``draftDetail.drafted``, because the espn_api library does not, and a draft that
ESPN has not flagged as complete is exactly the draft we need to read.

**The fixtures are recorded, so an expected value is one of three things.** A
value that churns with every recording — who is top of the free-agent wire, what
a player id is called — is read back out of the fixture rather than typed, so
re-recording costs nobody an afternoon of edits. A value that is a property of
Caroline's league — six teams, the lineup, the 90-second clock — is pinned as a
literal with the reason it is that number written beside it, because moving one
of those should fail a test. And a value that disagrees with what we believed
about ESPN is a finding: it is named in the docstring here and written up in
`docs/DECISIONS.md`. What none of them may become is `assert code == code`.
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
    fixture_player_name,
    load_espn_fixture,
)
from hal_mary.config import EspnConfig, load_settings
from hal_mary.espn import client as client_module
from hal_mary.espn.client import (
    DEFAULT_FREE_AGENT_SIZE,
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
    """The shape of the answer, against the values the fixture really carries.

    ``name``, ``draft_date`` and the season come out of the payload rather than
    being typed here: they are true of one recording and would have to be
    retyped after the next one, and a fixture set nobody dares re-record is how
    this suite ended up believing in a four-team league that does not exist.
    What *is* pinned below is the handful of facts that are properties of
    Caroline's league rather than of the day it was read.
    """
    fixture = load_espn_fixture("league_settings.json")
    raw = fixture["settings"]

    result = client_for(settings).league_settings()

    assert result["season"] == fixture["seasonId"]
    assert result["league_id"] == fixture["id"]
    assert result["name"] == raw["name"]
    assert result["draft_type"] == raw["draftSettings"]["type"]
    assert result["scoring_type"] == raw["scoringSettings"]["scoringType"]
    assert result["draft_date"].endswith("+00:00")


def test_a_draft_date_is_converted_from_epoch_milliseconds(settings, fake_espn):
    """ESPN sends epoch milliseconds; we store ISO-8601 UTC.

    Against a fixed epoch rather than the recorded one, so the conversion has a
    literal answer that no future recording moves — and so this does not become
    the test that re-derives the conversion it is checking.
    """
    payload = load_espn_fixture("league_settings.json")
    payload["settings"]["draftSettings"]["date"] = 1788915600000
    fake_espn.settings_payload = payload

    assert client_for(settings).league_settings()["draft_date"] == "2026-09-09T01:00:00+00:00"


@pytest.mark.parametrize("absent", [None, 0])
def test_a_league_with_no_draft_date_reports_none(settings, fake_espn, absent):
    """A league that has not scheduled its draft is not a league drafting in 1970."""
    payload = load_espn_fixture("league_settings.json")
    payload["settings"]["draftSettings"]["date"] = absent
    fake_espn.settings_payload = payload

    assert client_for(settings).league_settings()["draft_date"] is None


def test_league_settings_pins_the_shape_of_carolines_league(settings, fake_espn):
    """Six teams and a 90-second clock, which everything else is sized against.

    These two are not incidental values from one recording. Six teams times
    sixteen rounds is the 96-slot board ESPN pre-populates and
    ``draft_phase`` counts, and the 90-second pick clock is the reason the
    advisor runs with web tools off. If a re-record moves either of them,
    several other decisions in this project need revisiting rather than a test
    needing an edit.
    """
    raw = load_espn_fixture("league_settings.json")["settings"]

    assert client_for(settings).league_settings()["team_count"] == 6
    assert raw["draftSettings"]["timePerSelection"] == 90


def test_league_settings_roster_slots_are_named_and_drop_empty_slots(settings, fake_espn):
    """Caroline's real lineup, and the arithmetic that makes it 16 rounds.

    Nine of these start each week — QB, two RB, two WR, TE, the flex, a kicker
    and a defence — plus seven bench, which is sixteen players and therefore
    sixteen rounds of draft. ``IR`` is not drafted into, so it is outside that
    count. ESPN sends a count for all twenty-five of its slot ids; the zeroes
    are dropped here so nothing downstream renders a slot the league does not
    use.

    The flex is spelled ``RB/WR/TE``. It is not called ``FLEX`` anywhere on
    Caroline's screen, so it is not called that here either.
    """
    slots = client_for(settings).league_settings()["roster_slots"]

    assert slots == {
        "QB": 1,
        "RB": 2,
        "WR": 2,
        "TE": 1,
        "D/ST": 1,
        "K": 1,
        "BE": 7,
        "IR": 1,
        "RB/WR/TE": 1,
    }
    starters = sum(count for name, count in slots.items() if name not in ("BE", "IR"))
    assert starters + slots["BE"] == 16


def test_league_settings_carries_the_raw_settings_json(settings, fake_espn):
    """Everything we did not think to extract survives, byte for byte.

    Asserted against the whole ``settings`` block rather than two fields of it:
    the promise is that *nothing* is dropped, and a test that checks the trade
    deadline checks the trade deadline.
    """
    fixture = load_espn_fixture("league_settings.json")

    raw = json.loads(client_for(settings).league_settings()["raw_json"])

    assert raw == fixture["settings"]
    # A spot check that the block really does carry things the mapping ignores.
    assert raw["acquisitionSettings"]["acquisitionBudget"]
    assert raw["tradeSettings"]["deadlineDate"]


def test_league_settings_prefers_the_payload_over_the_configured_league(settings, fake_espn):
    """The season and league id are read, not assumed.

    ``league_settings`` falls back to ``settings.season`` / ``settings.league_id``
    when ESPN sends neither, and ``FIXTURE_ENV`` deliberately agrees with the
    fixture — so without this test the fallback would pass for the real read and
    a client pointed at the wrong season would look right.
    """
    fixture = load_espn_fixture("league_settings.json")
    misconfigured = settings.model_copy(update={"season": 1999, "league_id": 42})

    result = client_for(misconfigured).league_settings()

    assert result["season"] == fixture["seasonId"] != 1999
    assert result["league_id"] == fixture["id"] != 42


def test_league_settings_returns_plain_data(settings, fake_espn):
    result = client_for(settings).league_settings()
    # A round trip through JSON proves nothing library-shaped leaked out.
    assert json.loads(json.dumps(result)) == result


# --- teams -----------------------------------------------------------------


def test_teams_maps_the_fixture(settings, fake_espn):
    """Every team ESPN names, with the name and abbrev it gave them.

    Read out of the fixture rather than typed, because the recorded names are
    pseudonyms the scrubber assigns in first-seen order — "Team 3" is not a fact
    about anything, and retyping six of them after every recording is the chore
    that stops people recording.

    The count *is* pinned: six is the league's size, and it is the same six that
    make ESPN's 96-slot board.
    """
    fake_espn.league_fixture = "teams.json"
    fixture = load_espn_fixture("teams.json")["teams"]

    rows = client_for(settings).teams()

    assert len(rows) == 6
    assert [row["team_id"] for row in rows] == [team["id"] for team in fixture]
    assert [row["name"] for row in rows] == [team["name"] for team in fixture]
    assert [row["abbrev"] for row in rows] == [team["abbrev"] for team in fixture]


def test_a_teams_owner_is_the_member_espn_points_at(settings, fake_espn):
    """ESPN gives a team owner ids and the names in a separate `members` list.

    A team ESPN gives no owner reads as ``None``, and that is not hypothetical
    padding: this league is sized for six and four people have joined, so two
    team objects arrive with no ``owners`` key at all. ``None`` for those, a
    joined-up name for the rest.
    """
    fake_espn.league_fixture = "teams.json"
    fixture = load_espn_fixture("teams.json")
    members = {member["id"]: member for member in fixture["members"]}
    expected = {}
    for team in fixture["teams"]:
        owners = team.get("owners") or []
        names = [
            f"{members[owner]['firstName']} {members[owner]['lastName']}" for owner in owners
        ]
        expected[team["id"]] = ", ".join(names) or None

    rows = client_for(settings).teams()

    assert {row["team_id"]: row["owner"] for row in rows} == expected
    assert None in expected.values(), "the fixture must have an unclaimed team slot"
    assert any(expected.values()), "...and a claimed one, or this asserts nothing"


def test_a_teams_draft_slot_is_its_place_in_the_pick_order(settings, fake_espn):
    """The slot comes from ``draftSettings.pickOrder``, not from the team id.

    In the recorded fixture the drawn order happens to be ``[1, 2, 3, 4, 5, 6]``
    — team ids in ascending order — so every slot equals its team id and the
    fixture cannot tell the two apart. A shuffled order can, and this is the
    column the league page renders.

    It is still the *provisional* order. ``orderType`` is ``DRAFT_START``, so
    ESPN redraws it when the draft opens; ``teams.draft_slot`` is a placeholder
    and ``LeagueContext.draft_order`` is what the draft trusts.
    """
    fake_espn.league_fixture = "teams.json"
    payload = load_espn_fixture("league_settings.json")
    payload["settings"]["draftSettings"]["pickOrder"] = [4, 6, 1, 5, 3, 2]
    fake_espn.settings_payload = payload

    rows = client_for(settings).teams()

    assert {row["team_id"]: row["draft_slot"] for row in rows} == {
        4: 1,
        6: 2,
        1: 3,
        5: 4,
        3: 5,
        2: 6,
    }


# --- rosters ---------------------------------------------------------------


def test_rosters_are_empty_before_the_draft(settings, fake_espn):
    """The recorded truth, and the answer that must not be papered over.

    ``roster.json`` was recorded before this league drafted, so ESPN answers
    with six teams and not one rostered player. The synthetic fixture it
    replaced claimed six players, and a test written to that number would have
    to invent them back — which would say the mapping works against data ESPN
    has never sent.

    The mapping itself is covered next, against a roster built by hand out of
    real player objects. The two together are the honest pair: this one says
    what ESPN really answers today, that one says what the code does with a
    roster once there is one.
    """
    fixture_teams = load_espn_fixture("roster.json")["teams"]
    assert len(fixture_teams) == 6, "the fixture must still describe a whole league"
    assert all(not team.get("roster", {}).get("entries") for team in fixture_teams)

    assert client_for(settings).rosters() == []


def test_rosters_is_empty_when_no_team_has_a_roster(settings, fake_espn):
    fake_espn.league_fixture = "teams.json"
    assert client_for(settings).rosters() == []


#: Lineup slot ids, from ESPN's own numbering. ``23`` is this league's flex,
#: which is spelled ``RB/WR/TE`` and never ``FLEX``.
SLOT_RB, SLOT_WR, SLOT_FLEX, SLOT_BENCH = 2, 4, 23, 20


def _drafted_league(placements):
    """The recorded league with real players placed onto teams by hand.

    ``placements`` is ``{team_id: [(free-agent index, lineup slot id), ...]}``.
    The player objects are lifted whole out of ``free_agents.json``, so every
    field the library reads — position eligibility, pro team id, injury status —
    is ESPN's own; only *where the player is sitting* is invented, because that
    is the one thing a pre-draft league cannot be recorded saying.
    """
    payload = load_espn_fixture("roster.json")
    pool = load_espn_fixture("free_agents.json")["players"]
    for team in payload["teams"]:
        entries = [
            {
                "playerId": pool[index]["id"],
                "lineupSlotId": slot_id,
                "acquisitionType": "DRAFT",
                "playerPoolEntry": pool[index],
            }
            for index, slot_id in placements.get(team["id"], [])
        ]
        team["roster"] = {"entries": entries}
    return payload, pool


def test_rosters_map_a_drafted_roster(settings, fake_espn):
    """What every in-season job reads: one row per rostered player, all teams.

    Names, positions and pro teams are checked against the player objects the
    payload was built from rather than typed out, so this survives a re-record.
    """
    payload, pool = _drafted_league(
        {1: [(0, SLOT_RB), (1, SLOT_WR), (2, SLOT_BENCH)], 2: [(3, SLOT_FLEX)]}
    )
    fake_espn.league_payload = payload

    rows = client_for(settings).rosters()

    assert [row["team_id"] for row in rows] == [1, 1, 1, 2]
    assert [row["player_id"] for row in rows] == [pool[index]["id"] for index in range(4)]
    assert [row["name"] for row in rows] == [
        pool[index]["player"]["fullName"] for index in range(4)
    ]
    assert [row["slot"] for row in rows] == ["RB", "WR", "BE", "RB/WR/TE"]
    assert all(row["position"] for row in rows)
    assert all(row["pro_team"] for row in rows)


def test_a_rostered_players_injury_status_is_espns_own(settings, fake_espn):
    """The field the bye and lineup checks are built on, carried unchanged.

    Both a healthy player and a hurt one, taken from the recorded pool, because
    a test that only ever sees ``ACTIVE`` cannot tell "carried through" from
    "hardcoded".
    """
    pool = load_espn_fixture("free_agents.json")["players"]
    healthy = next(i for i, p in enumerate(pool) if p["player"].get("injuryStatus") == "ACTIVE")
    hurt = next(
        i
        for i, p in enumerate(pool)
        if p["player"].get("injuryStatus") not in (None, "ACTIVE")
    )
    payload, _ = _drafted_league({1: [(healthy, SLOT_RB), (hurt, SLOT_BENCH)]})
    fake_espn.league_payload = payload

    rows = client_for(settings).rosters()

    assert [row["injury_status"] for row in rows] == [
        "ACTIVE",
        pool[hurt]["player"]["injuryStatus"],
    ]
    assert rows[1]["injury_status"] != "ACTIVE"


# --- free agents -----------------------------------------------------------


def test_free_agents_maps_the_fixture(settings, fake_espn):
    """Every available player ESPN sent, in the order ESPN sent them.

    Which player is top of the wire is the definition of a churning value — it
    changes with every recording and with every waiver run — so the ids and
    names are read back out of the fixture. What is asserted about them is what
    the waiver job actually depends on: the order is ESPN's own (most-owned
    first, which is why nothing here re-sorts), and every row is complete.
    """
    fixture = load_espn_fixture("free_agents.json")["players"]

    rows = client_for(settings).free_agents()

    assert [row["player_id"] for row in rows] == [entry["id"] for entry in fixture]
    assert [row["name"] for row in rows] == [
        entry["player"]["fullName"] for entry in fixture
    ]
    assert all(row["position"] and row["pro_team"] for row in rows)
    owned = [row["percent_owned"] for row in rows]
    assert all(isinstance(value, float) and 0 <= value <= 100 for value in owned)
    assert owned == sorted(owned, reverse=True), "ESPN's own most-owned-first order"


def test_free_agents_ask_for_as_many_players_as_the_default_says(settings, fake_espn):
    """The pull size is a real bound on what the waiver job can ever consider."""
    assert len(load_espn_fixture("free_agents.json")["players"]) == DEFAULT_FREE_AGENT_SIZE
    assert len(client_for(settings).free_agents()) == DEFAULT_FREE_AGENT_SIZE


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
        "player_name": fixture_player_name(4362628),
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
    """Every id ESPN's player list carries can be named, and nothing else can.

    The whole list, not two spot checks: the map is what turns a pick into a
    sentence Caroline can read, and one id it cannot name is one pick that says
    "player 4426515" on the night. Which id belongs to whom is ESPN's business
    and changes with every recording — the synthetic fixtures had 4426515 down
    as Sam LaPorta, and ESPN says Puka Nacua — so the expected names come out of
    the fixture.
    """
    pro_players = load_espn_fixture("pro_players.json")

    names = client_for(settings).player_name_map()

    assert names == {player["id"]: player["fullName"] for player in pro_players} | {
        entry["id"]: entry["player"]["fullName"]
        for entry in load_espn_fixture("free_agents.json")["players"]
    }
    assert 9999999 not in names
    assert all(isinstance(key, int) for key in names)
    assert all(isinstance(value, str) and value for value in names.values())


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
    assert client.draft_picks()[0]["player_name"] == fixture_player_name(4362628)


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


def _league_reporting_week(week: int):
    """The recorded league, moved to a week ESPN says is in progress.

    ``scoringPeriodId`` is the field ``espn_api`` reads and clamps to
    ``status.finalScoringPeriod``; ``latestScoringPeriod`` is moved with it so
    the payload is internally consistent rather than a shape ESPN never sends.
    """
    payload = load_espn_fixture("roster.json")
    payload["scoringPeriodId"] = week
    payload["status"]["latestScoringPeriod"] = week
    return payload


def test_current_week_reads_the_scoring_period(settings, fake_espn):
    """Nothing else in the schema knows which NFL week it is.

    The bye-week action producer asks "is this player on bye *right now*", and
    every in-season job asks "what week is it". The only honest answer comes
    from ESPN rather than from arithmetic over a calendar the code would have to
    hardcode; a wrong answer flags the wrong players — or nobody.

    The week is fabricated here because it has to be: the recorded fixtures came
    off a league whose season had not started, so no committed payload reports a
    week at all. That is the subject of the next two tests.
    """
    fake_espn.league_payload = _league_reporting_week(5)

    assert client_for(settings).current_week() == 5


def test_current_week_is_none_before_the_season_starts(settings, fake_espn):
    """The finding the first real recording produced.

    ESPN reports ``scoringPeriodId: 0`` and ``status.latestScoringPeriod: 0``
    for a league whose season has not kicked off. The synthetic fixture this
    replaced claimed week 1, so the suite had never once seen the state the
    league is actually in the whole time hal-mary was being built.

    ``None`` is the right answer, and it is the answer every caller already
    handles: ``jobs/season.current_week`` falls back to the database and then to
    the week the model established from the live NFL schedule.
    """
    payload = load_espn_fixture("roster.json")
    assert payload["scoringPeriodId"] == 0
    assert payload["status"]["latestScoringPeriod"] == 0

    assert client_for(settings).current_week() is None


@pytest.mark.parametrize("reported", [0, -1])
def test_a_scoring_period_of_zero_never_becomes_week_zero(settings, fake_espn, reported):
    """Week 0 is the dangerous answer, not `None`.

    ``None`` propagates: every caller has a fallback and the bye check reports
    that it could not be made. A ``0`` propagates too, and silently — it is a
    number, so it reaches ``season.bye_weeks`` as a week, matches no player's
    bye, and the lineup card says nobody is on a bye. That is the one sentence
    Caroline must not be told wrongly.
    """
    fake_espn.league_payload = _league_reporting_week(reported)

    assert client_for(settings).current_week() is None


def test_current_week_without_cookies_raises_rather_than_guessing(anonymous_settings, no_network):
    """No cookies at all is a configuration problem, not a week ESPN withheld."""
    with pytest.raises(EspnAuthError):
        client_for(anonymous_settings).current_week()


def test_current_week_is_none_rather_than_a_guess_when_espn_will_not_say(settings, fake_espn):
    """No week beats a guessed one. A calendar-derived week would silently move
    the bye check onto the wrong players, which is worse than not making it."""
    fake_espn.status = 401
    assert client_for(settings).current_week() is None
