"""Tests for ``scripts/record_espn_fixtures.py``.

The committed fixtures are synthetic. This script replaces them with the real
thing the moment cookies exist, which makes it the one piece of code in the
repo whose job is to take a payload full of live credentials and write it into
git. The scrubber is therefore tested harder than the recording.
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
    """
    scrubbed = recorder.scrub(load_fixture("roster.json"))

    for team in scrubbed["teams"]:
        assert re.fullmatch(r"TM\d+", team["abbrev"]), team["abbrev"]
        assert team["logo"] == recorder.REDACTED
        assert team["name"].startswith("Team ")
        assert team["location"].startswith("Team ")
        assert team["nickname"].startswith("Team ")

    for member in scrubbed["members"]:
        assert member["firstName"].startswith("Person ")
        assert member["lastName"].startswith("Person ")
        assert member["displayName"].startswith("Person ")
        assert SWID_SHAPE.fullmatch(member["id"])

    # Owner ids still point at the members they belong to.
    owners = {owner for team in scrubbed["teams"] for owner in team["owners"]}
    assert owners == {member["id"] for member in scrubbed["members"]}

    # And the league keeps its own name, which is the point of re-recording.
    assert scrubbed["settings"]["name"] == "The Gridiron Gauntlet"


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


@pytest.mark.parametrize(
    ("drafted", "picks", "expected"),
    [
        (False, [], "draft_detail_empty.json"),
        (False, [{"overallPickNumber": 1}], "draft_detail_partial.json"),
        (True, [{"overallPickNumber": 1}], "draft_detail_full.json"),
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
