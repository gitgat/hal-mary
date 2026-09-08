"""Tests for ``scripts/record_espn_fixtures.py``.

This script is the one piece of code in the repo whose job is to take a payload
full of live credentials and write it into git, so the scrubber is tested harder
than the recording. The committed fixtures came out of it.

Every example payload here is invented. The one that was not — an early draft
used the league's real id to demonstrate that the league id gets substituted —
is why `test_no_tracked_file_carries_the_real_league_id` exists.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "record_espn_fixtures.py"


def load_fixture(name):
    return json.loads((REPO / "tests" / "fixtures" / "espn" / name).read_text(encoding="utf-8"))


def _load_script():
    """Import the script by path: scripts/ is an operator tool, not a package."""
    spec = importlib.util.spec_from_file_location("record_espn_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their annotations through sys.modules, so register the
    # module before executing it or FixtureSpec cannot be built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recorder():
    return _load_script()


REAL_S2 = "AEBxyzVeryLongOpaqueCookieValue%2Bwith%2Fpadding%3D" * 3
REAL_SWID = "{1A2B3C4D-5E6F-7788-99AA-BBCCDDEEFF00}"
SWID_SHAPE = re.compile(r"\{[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}")
OTHER_SWID = "{FFEEDDCC-BBAA-9988-7766-554433221100}"


# --- the scrubber ----------------------------------------------------------


def test_the_live_cookies_never_survive_the_scrub(recorder):
    payload = {
        "espn_s2": REAL_S2,
        "note": f"set-cookie: SWID={REAL_SWID}; espn_s2={REAL_S2}",
        "members": [{"id": REAL_SWID}],
    }

    scrubbed = json.dumps(recorder.scrub(payload, secrets=[REAL_S2, REAL_SWID]))

    assert REAL_S2 not in scrubbed
    assert REAL_SWID not in scrubbed


def test_swid_like_values_are_pseudonymised_consistently(recorder):
    """Team-to-owner links must survive, or the fixture stops making sense."""
    payload = {
        "members": [{"id": REAL_SWID}, {"id": OTHER_SWID}],
        "teams": [{"owners": [REAL_SWID]}, {"owners": [OTHER_SWID]}],
    }

    scrubbed = recorder.scrub(payload)

    first = scrubbed["members"][0]["id"]
    second = scrubbed["members"][1]["id"]
    assert first not in (REAL_SWID, OTHER_SWID)
    assert first != second
    assert scrubbed["teams"][0]["owners"] == [first]
    assert scrubbed["teams"][1]["owners"] == [second]


def test_real_peoples_names_never_survive_the_scrub(recorder):
    """Irreversible once pushed, so it must be impossible to get wrong.

    A real ESPN payload names every member of the league. Recording it verbatim
    would put Caroline's leaguemates into a public git history for good.
    """
    payload = {
        "members": [
            {"id": REAL_SWID, "firstName": "Caroline", "lastName": "Reed",
             "displayName": "creed88"},
        ]
    }

    scrubbed = recorder.scrub(payload)
    member = scrubbed["members"][0]

    assert "Caroline" not in json.dumps(scrubbed)
    assert "Reed" not in json.dumps(scrubbed)
    assert "creed88" not in json.dumps(scrubbed)
    assert member["firstName"] and member["lastName"] and member["displayName"]


def test_people_are_pseudonymised_consistently(recorder):
    """The same person must read as the same person across every fixture field."""
    payload = {
        "a": {"firstName": "Caroline"},
        "b": {"displayName": "Caroline"},
        "c": {"firstName": "Dana"},
    }

    scrubbed = recorder.scrub(payload)

    assert scrubbed["a"]["firstName"] == scrubbed["b"]["displayName"]
    assert scrubbed["a"]["firstName"] != scrubbed["c"]["firstName"]


def test_team_names_are_pseudonymised(recorder):
    """League members name their teams after themselves more often than not."""
    payload = {
        "teams": [
            {
                "id": 1,
                "abbrev": "CARO",
                "owners": [REAL_SWID],
                "location": "Caroline's",
                "nickname": "Chaos",
                "name": "Caroline's Chaos",
                "logo": "https://example.com/u/caroline-reed/avatar.png",
            }
        ]
    }

    scrubbed = recorder.scrub(payload)
    text = json.dumps(scrubbed)

    assert "Caroline" not in text
    assert "Chaos" not in text
    # ESPN derives abbrev from the team name, so "Caroline's Chaos" becomes CARO
    # and survives every other pseudonym.
    assert "CARO" not in text
    assert scrubbed["teams"][0]["abbrev"]
    # A custom logo is a user-supplied URL whose path can carry a name or a
    # profile-image id, and it has a :// so no token pattern catches it.
    assert scrubbed["teams"][0]["logo"] == recorder.REDACTED


def test_abbrevs_are_pseudonymised_consistently(recorder):
    payload = {"teams": [{"abbrev": "CARO"}, {"abbrev": "DANA"}, {"abbrev": "CARO"}]}

    teams = recorder.scrub(payload)["teams"]

    assert teams[0]["abbrev"] == teams[2]["abbrev"]
    assert teams[0]["abbrev"] != teams[1]["abbrev"]


def test_a_team_carrying_only_a_name_still_loses_it(recorder):
    """2023-and-later payloads send `name` with no owners, roster or playoffSeed."""
    payload = {"teams": [{"id": 1, "abbrev": "CARO", "name": "Caroline's Chaos"}]}

    assert "Caroline" not in json.dumps(recorder.scrub(payload))


def test_free_form_text_keys_are_redacted(recorder):
    """No view we record carries these today; the failure mode if one is added is silent."""
    payload = {
        "message": "Dana said she would veto the trade",
        "text": "see you at Caroline's on Sunday",
        "note": "Marcus owes the pot $20",
    }

    assert set(recorder.scrub(payload).values()) == {recorder.REDACTED}


def test_the_league_and_division_names_are_kept(recorder):
    """Only team-shaped objects lose their `name`; the league keeps its own."""
    payload = {
        "settings": {
            "name": "The Gridiron Gauntlet",
            "scheduleSettings": {"divisions": [{"id": 0, "name": "East"}]},
        }
    }

    assert recorder.scrub(payload) == payload


def test_email_shaped_keys_are_redacted(recorder):
    payload = {"email": "caroline@example.com", "mailingAddress": "1 Main St"}

    assert set(recorder.scrub(payload).values()) == {recorder.REDACTED}


def test_cookie_like_keys_are_redacted_whatever_they_hold(recorder):
    payload = {
        "espn_s2": "anything",
        "SWID": "anything",
        "authToken": "anything",
        "sessionSecret": "anything",
        "password": "anything",
    }

    scrubbed = recorder.scrub(payload)

    assert set(scrubbed.values()) == {recorder.REDACTED}


def test_long_opaque_tokens_are_redacted_even_under_an_innocent_key(recorder):
    payload = {"blob": REAL_S2}

    assert recorder.scrub(payload)["blob"] == recorder.REDACTED


def test_the_scrubber_walks_nested_lists_and_dicts(recorder):
    payload = {"a": [{"b": [{"SWID": REAL_SWID}]}]}

    assert recorder.scrub(payload)["a"][0]["b"][0]["SWID"] == recorder.REDACTED


def test_ordinary_league_data_is_left_alone(recorder):
    """Everything that is neither a credential nor a person stays readable."""
    payload = {
        "id": 1234567,
        "seasonId": 2025,
        "scoringPeriodId": 4,
        "settings": {
            "name": "The Gridiron Gauntlet",
            "size": 10,
            "draftSettings": {"type": "SNAKE", "pickOrder": [3, 1, 4, 2]},
        },
        "teams": [{"id": 1, "playoffSeed": 2, "record": {"overall": {"wins": 3}}}],
    }

    assert recorder.scrub(payload) == payload


ADVERSARIAL_LEAGUE = {
    "id": 1234567,
    "settings": {"name": "The Gridiron Gauntlet", "size": 4},
    "members": [
        {"id": REAL_SWID, "firstName": "Caroline", "lastName": "Reed", "displayName": "creed88"},
    ],
    "teams": [
        # Fully populated, the shape roster.json has.
        {
            "id": 1,
            "abbrev": "CARO",
            "owners": [REAL_SWID],
            "location": "Caroline's",
            "nickname": "Chaos",
            "name": "Caroline's Chaos",
            "logo": "https://example.com/u/caroline-reed/avatar.png",
        },
        # Name and abbrev only.
        {"id": 2, "abbrev": "DANA", "name": "Dana's Destroyers"},
        # Name only, and nothing else at all. No marker key can see this one.
        {"id": 3, "name": "Bare Name Only"},
    ],
    # ESPN nests abbreviated team references here, outside any `teams` list.
    "schedule": [{"home": {"teamId": 1, "name": "Nested Ref Name"}}],
    "communication": {
        "topics": [{"messages": [{"id": 1, "text": "Marcus owes the pot $20"}]}],
    },
    "contact": {"email": "caroline@example.com"},
}

#: Every real value in ADVERSARIAL_LEAGUE that must not survive.
IDENTIFYING = (
    "Caroline",
    "Reed",
    "creed88",
    "CARO",
    "DANA",
    "Chaos",
    "Destroyers",
    "Bare Name Only",
    "Nested Ref Name",
    "Marcus",
    "caroline@example.com",
    "example.com",
    REAL_SWID,
)


def test_nothing_identifying_survives_an_adversarial_payload(recorder):
    """"What could ESPN send", as opposed to "what did ESPN send".

    The real-payload test below is the better test of the two for catching
    fields I did not think about, but every team object in that fixture is
    fully populated, so it cannot reach a team that carries a name and nothing
    else — and that is exactly the shape that kept its real name through two
    rounds of fixes.
    """
    scrubbed = json.dumps(recorder.scrub(ADVERSARIAL_LEAGUE, secrets=[REAL_S2, REAL_SWID]))

    leaked = [value for value in IDENTIFYING if value in scrubbed]
    assert leaked == []


def test_a_team_is_a_team_because_of_where_it_sits_not_what_it_carries(recorder):
    """Position, not shape.

    Marker-sniffing asks "does this dict look like a team", which is a guess and
    was wrong twice. Anything under a key that holds teams *is* a team, whatever
    fields it happens to carry.
    """
    scrubbed = recorder.scrub({"teams": [{"id": 3, "name": "Bare Name Only"}]})

    assert scrubbed["teams"][0]["name"].startswith("Team ")
    assert scrubbed["teams"][0]["id"] == 3


def test_a_nested_team_reference_is_a_team_too(recorder):
    """`home` and `away` hold one team each, not a list of them."""
    payload = {"schedule": [{"home": {"teamId": 1, "name": "Caroline's Chaos"}}]}

    scrubbed = recorder.scrub(payload)

    assert scrubbed["schedule"][0]["home"]["name"].startswith("Team ")
    assert scrubbed["schedule"][0]["home"]["teamId"] == 1


def test_our_own_swid_is_pseudonymised_like_everyone_elses(recorder):
    """The recorder passes Caroline's SWID as a secret, and it is also a member id.

    Redacting it while pseudonymising the others would single her out and break
    the owner-to-member link for her team alone.
    """
    payload = {
        "members": [{"id": REAL_SWID}, {"id": OTHER_SWID}],
        "teams": [{"id": 1, "owners": [REAL_SWID]}],
    }

    scrubbed = recorder.scrub(payload, secrets=[REAL_SWID])

    ours = scrubbed["members"][0]["id"]
    assert REAL_SWID not in json.dumps(scrubbed)
    assert ours != recorder.REDACTED
    assert ours != scrubbed["members"][1]["id"]
    assert scrubbed["teams"][0]["owners"] == [ours]


def test_the_scrubber_reaches_every_identity_field_of_a_real_league_payload(recorder):
    """Run it over a whole league response, not a payload shaped by my assumptions.

    The `abbrev` and `logo` leaks were both invisible to hand-written test
    payloads and both present in this fixture. Asserting against the real shape
    is what catches the next one.

    **This test used to assert a field ESPN does not send.** It required every
    team to carry a pseudonymised `location` and `nickname`, which is the shape
    the hand-built fixtures had and the shape `espn_api`'s `Team` still falls
    back to. The real 2026 payload carries neither: a team's display name is one
    `name` field, and `location`/`nickname` are the older spelling. So the test
    passed against invented data and raised `KeyError` the first time it met a
    real league — a test that could only ever be green about a fiction.
    """
    fixture = load_fixture("roster.json")
    scrubbed = recorder.scrub(fixture)

    for team in scrubbed["teams"]:
        assert re.fullmatch(r"TM\d+", team["abbrev"]), team["abbrev"]
        assert team["logo"] == recorder.REDACTED
        assert team["name"].startswith("Team ")
        # The fields ESPN actually sends, asserted as a set so a new one shows
        # up here rather than going through unscrubbed.
        assert {"location", "nickname"}.isdisjoint(team), (
            "ESPN has started sending location/nickname again; they are "
            "pseudonymised, but this test no longer describes the payload"
        )
        # `primaryOwner` is a SWID, and an unclaimed slot omits the key entirely.
        if team.get("primaryOwner") is not None:
            assert SWID_SHAPE.fullmatch(team["primaryOwner"])

    for member in scrubbed["members"]:
        assert member["firstName"].startswith("Person ")
        assert member["lastName"].startswith("Person ")
        assert member["displayName"].startswith("Person ")
        assert SWID_SHAPE.fullmatch(member["id"])

    # Owner ids still point at the members they belong to. A team slot nobody
    # has claimed — this league has six slots and four members — omits `owners`
    # and `primaryOwner` altogether rather than sending them empty, so an
    # unowned team contributes nothing here rather than raising.
    owners = {owner for team in scrubbed["teams"] for owner in team.get("owners") or []}
    assert owners == {member["id"] for member in scrubbed["members"]}
    assert any(team.get("primaryOwner") for team in scrubbed["teams"]), (
        "the fixture must contain at least one owned team, or the SWID check "
        "above asserts nothing"
    )

    # And the league keeps its own name, which is the point of re-recording:
    # taken from the input, not asserted as a literal, so re-recording a league
    # that has been renamed does not turn this into a chore.
    assert scrubbed["settings"]["name"] == fixture["settings"]["name"]
    assert not scrubbed["settings"]["name"].startswith("Team ")


def test_a_team_slot_nobody_has_claimed_still_scrubs(recorder):
    """ESPN ships a full-size league before anyone joins it.

    This league is set to six teams and four people have joined, so two team
    objects arrive with no `owners` and no `primaryOwner` key at all — ESPN
    omits them rather than sending them empty. They still carry a name, an
    abbrev and a logo, and all three are identity — so the scrubber must not
    reach any of them through `owners`.
    """
    payload = {
        "teams": [
            {
                "id": 2,
                "abbrev": "CARO",
                "name": "Caroline's Chaos",
                "logo": "https://example.com/u/caroline-reed/avatar.png",
            }
        ]
    }

    scrubbed = recorder.scrub(payload)

    assert "Caroline" not in json.dumps(scrubbed)
    assert "CARO" not in json.dumps(scrubbed)
    assert scrubbed["teams"][0]["logo"] == recorder.REDACTED


def test_an_nfl_players_full_name_survives_but_his_first_name_does_not(recorder):
    """The scrubber cannot tell a leaguemate from a running back, and errs closed.

    `firstName`/`lastName`/`displayName` are pseudonymised wherever they appear,
    and ESPN spells a *player's* name into those same fields — so the recorded
    `free_agents.json` has Jahmyr Gibbs with a `firstName` of "Person 1". That
    is over-scrubbing a public figure, which costs nothing: nothing in hal-mary
    reads those two fields, and the alternative is a rule that has to guess
    which dict is a person and which is an athlete.

    `fullName` is deliberately *not* on that list, and that is what makes the
    fixtures worth having: `player_name_map()` is the whole reason to record a
    player list, and a board of "Person 1" would pin nothing. No member object
    ESPN sends carries a `fullName`, so keeping it leaks no leaguemate.
    """
    payload = {
        "players": [
            {"id": 4429795, "fullName": "Jahmyr Gibbs", "firstName": "Jahmyr", "lastName": "Gibbs"}
        ]
    }

    scrubbed = recorder.scrub(payload)

    assert scrubbed["players"][0]["fullName"] == "Jahmyr Gibbs"
    assert scrubbed["players"][0]["firstName"].startswith("Person ")
    assert scrubbed["players"][0]["lastName"].startswith("Person ")


# --- recording -------------------------------------------------------------


def test_every_committed_fixture_can_be_re_recorded(recorder):
    """A fixture the recorder does not know about would silently go stale."""
    committed = {path.name for path in (REPO / "tests" / "fixtures" / "espn").glob("*.json")}

    assert committed <= recorder.recordable_filenames()


def test_recording_writes_scrubbed_files_and_no_cookies(recorder, tmp_path):
    payloads = {
        "mSettings": {"id": 1, "settings": {"name": "L"}, "swid": REAL_SWID},
        "league": {
            "id": 1,
            "seasonId": 2025,
            "scoringPeriodId": 1,
            "members": [{"id": REAL_SWID}],
            "teams": [{"id": 1, "roster": {"entries": [{"playerId": 7}]}}],
        },
        "kona_player_info": {"players": []},
        "players_wl": [{"id": 7, "fullName": "A Player", "proTeamId": 1}],
        "proTeamSchedules_wl": {"settings": {"proTeams": []}},
        "mPositionalRatings": {"positionAgainstOpponent": {"positionalRatings": {}}},
        "mDraftDetail": {"draftDetail": {"drafted": False, "picks": []}},
    }

    written = recorder.record(
        fetch=lambda spec: payloads[spec.key],
        out_dir=tmp_path,
        secrets=[REAL_S2, REAL_SWID],
    )

    assert written
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert REAL_SWID not in text
        assert REAL_S2 not in text


def test_recording_empties_the_rosters_in_the_teams_fixture(recorder, tmp_path):
    league = {
        "id": 1,
        "seasonId": 2025,
        "scoringPeriodId": 1,
        "teams": [{"id": 1, "roster": {"entries": [{"playerId": 7}]}}],
    }
    payloads = {
        "mSettings": {"settings": {}},
        "league": league,
        "kona_player_info": {"players": []},
        "players_wl": [],
        "proTeamSchedules_wl": {"settings": {"proTeams": []}},
        "mPositionalRatings": {"positionAgainstOpponent": {"positionalRatings": {}}},
        "mDraftDetail": {"draftDetail": {"drafted": False, "picks": []}},
    }

    recorder.record(fetch=lambda spec: payloads[spec.key], out_dir=tmp_path)

    teams = json.loads((tmp_path / "teams.json").read_text(encoding="utf-8"))
    roster = json.loads((tmp_path / "roster.json").read_text(encoding="utf-8"))
    assert teams["teams"][0]["roster"]["entries"] == []
    assert roster["teams"][0]["roster"]["entries"] == [{"playerId": 7}]


MADE = {"overallPickNumber": 1, "playerId": 4362628}
#: What ESPN really writes into an unmade slot on its pre-populated board.
UNMADE = {"overallPickNumber": 1, "playerId": -1}


@pytest.mark.parametrize(
    ("drafted", "picks", "expected"),
    [
        (False, [], "draft_detail_empty.json"),
        # A board of placeholders is not a draft in progress; recording it over
        # draft_detail_partial.json would replace a real mid-draft payload with
        # a pre-draft one and nobody would notice until the fixture was needed.
        (False, [UNMADE], "draft_detail_prepopulated_real_league.json"),
        (False, [MADE], "draft_detail_partial.json"),
        (False, [UNMADE, MADE], "draft_detail_partial.json"),
        (True, [MADE], "draft_detail_full.json"),
    ],
)
def test_the_draft_is_recorded_into_the_file_that_matches_its_state(
    recorder, tmp_path, drafted, picks, expected
):
    payloads = {
        "mSettings": {"settings": {}},
        "league": {"id": 1, "seasonId": 2025, "scoringPeriodId": 1, "teams": []},
        "kona_player_info": {"players": []},
        "players_wl": [],
        "proTeamSchedules_wl": {"settings": {"proTeams": []}},
        "mPositionalRatings": {"positionAgainstOpponent": {"positionalRatings": {}}},
        "mDraftDetail": {"draftDetail": {"drafted": drafted, "picks": picks}},
    }

    written = recorder.record(fetch=lambda spec: payloads[spec.key], out_dir=tmp_path)

    assert (tmp_path / expected) in written


#: Stands in for the real league id everywhere below.
#:
#: **Not the real one, deliberately.** The first draft of these tests used the
#: league's actual id as the example payload — the leak, written into the tests
#: that exist to prevent it. A run of digits does not look like a secret the way
#: `ESPN_S2=AEB...` does, which is exactly why it nearly went in.
#: `test_no_tracked_file_carries_the_real_league_id` in `test_project_files.py`
#: is what caught it, and what will catch the next one.
REAL_LEAGUE_ID = 987654321


def test_the_league_id_is_replaced_with_the_fixture_league_id(recorder):
    """The league id is an identifier for a private league, like a SWID.

    `CLAUDE.md` names it alongside leaguemates' names as the thing that must
    never reach git: `memory/league.md` is gitignored for carrying "real
    leaguemates' names and the league id". The scrubber pseudonymised the names
    and left the number, so the first real recording would have committed the
    one identifier that lets a stranger look the league up — and a later commit
    cannot take it back out of history.
    """
    payload = {
        "id": REAL_LEAGUE_ID,
        "gameId": 1,
        "settings": {"name": "My 2026 League"},
        "draftDetail": {"picks": [{"leagueId": REAL_LEAGUE_ID, "playerId": 12}]},
        "note": f"see league {REAL_LEAGUE_ID} for details",
    }

    scrubbed = recorder.scrub(payload, secrets=[], league_id=REAL_LEAGUE_ID)

    flat = json.dumps(scrubbed)
    assert str(REAL_LEAGUE_ID) not in flat, flat
    assert scrubbed["id"] == recorder.FIXTURE_LEAGUE_ID
    assert scrubbed["draftDetail"]["picks"][0]["leagueId"] == recorder.FIXTURE_LEAGUE_ID
    # The rest of the payload is untouched: this is a substitution, not a purge.
    assert scrubbed["gameId"] == 1
    assert scrubbed["draftDetail"]["picks"][0]["playerId"] == 12


def test_an_unrelated_number_that_merely_looks_like_an_id_survives(recorder):
    """Only the league's own id is replaced, not every nine-digit number.

    Player ids are nine digits too, and collapsing those would break every
    fixture that maps a pick to a player.
    """
    payload = {
        "id": REAL_LEAGUE_ID,
        "players": [{"id": 4429795}, {"id": REAL_LEAGUE_ID + 1}],
    }

    scrubbed = recorder.scrub(payload, secrets=[], league_id=REAL_LEAGUE_ID)

    assert scrubbed["players"][0]["id"] == 4429795
    assert scrubbed["players"][1]["id"] == REAL_LEAGUE_ID + 1
