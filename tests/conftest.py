"""Shared test plumbing, chiefly a fake ESPN that never touches the network.

Two very different things are mocked here, because hal-mary talks to ESPN two
different ways:

* the ``espn_api`` library, which uses ``requests`` — faked by monkeypatching
  ``espn_api.requests.espn_requests.requests.get`` so the *real* library code
  parses our fixtures. Mocking at that seam is deliberate: it means a fixture
  that the real library cannot read fails the suite here, rather than at 6am on
  draft day.
* the raw ``draftDetail`` endpoint, which hal-mary calls itself over ``httpx``
  — faked with ``httpx.MockTransport``.

Every fixture under ``tests/fixtures/espn`` is **recorded** from Caroline's real
league by ``scripts/record_espn_fixtures.py`` and scrubbed on the way out: real
names, SWIDs, team names, logos and the league id do not survive. Two things
follow from *when* it was recorded, and both are load-bearing in the tests:

* it was recorded **before the draft**, so every roster is empty and
  ``draft_detail_prepopulated_real_league.json`` is 96 unfilled slots;
* it was recorded **before the season**, so ``scoringPeriodId`` is ``0`` and
  ``current_week()`` is correctly ``None``.

The hand-built ``draft_detail_partial.json`` and ``draft_detail_full.json``
remain: a draft in progress cannot be recorded from a league that has not
drafted, and those two exist to exercise picks landing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from espn_api.requests import espn_requests

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "espn"

#: The league the fixtures describe — **read out of the fixtures**, not typed.
#:
#: ``FIXTURE_ENV`` below is the environment a test runs against, and it describes
#: the same league the fake ESPN answers about. Typed literals put that agreement
#: in someone's hands: re-recording a league in a new season left the environment
#: on the old one, and the first thing that failed was a test whose subject was
#: something else entirely. Deriving them is the difference between a fixture set
#: that gets refreshed and one nobody dares touch.
#:
#: It does mean `season` and `league_id` now agree on both sides of the read, so
#: `league_settings()`'s fallback to `settings.*` would pass for the real thing.
#: `test_league_settings_prefers_the_payload_over_the_configured_league` is what
#: stops that; do not delete it on the grounds that it looks redundant.
#:
#: The league id is not the real one — ``record_espn_fixtures`` substitutes its
#: own ``FIXTURE_LEAGUE_ID`` on the way out.
_LEAGUE_SETTINGS = json.loads(
    (FIXTURE_DIR / "league_settings.json").read_text(encoding="utf-8")
)
FIXTURE_LEAGUE_ID = _LEAGUE_SETTINGS["id"]
FIXTURE_SEASON = _LEAGUE_SETTINGS["seasonId"]
#: Caroline's team in the fixtures.
FIXTURE_TEAM_ID = 1

FIXTURE_ENV = {
    "ESPN_S2": "fake-espn-s2-cookie",
    "SWID": "{00000000-0000-0000-0000-000000000001}",
    "LEAGUE_ID": str(FIXTURE_LEAGUE_ID),
    "TEAM_ID": str(FIXTURE_TEAM_ID),
    "SEASON": str(FIXTURE_SEASON),
    "WEB_PASSWORD": "not-a-real-password",
}


def load_espn_fixture(name: str) -> Any:
    """Read one committed ESPN fixture."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture_player_name(player_id: int) -> str:
    """The name ESPN's own recorded player list gives this id.

    Every assertion about a player's name goes through here rather than naming
    a player. The hand-built draft fixtures carry real ESPN player ids, and the
    recorded player list is what decides who those ids *are* — the synthetic
    fixtures had 4426515 down as Sam LaPorta and ESPN says Puka Nacua. Reading
    the answer out of the same list the client reads is not circular: the client
    goes through ``espn_api``'s parsing and its own name map, and this goes
    straight to the JSON.
    """
    for player in load_espn_fixture("pro_players.json"):
        if player["id"] == player_id:
            return player["fullName"]
    raise AssertionError(f"player {player_id} is not in the recorded player list")


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeEspnApi:
    """Serves committed fixtures to whatever ``espn_api`` asks for.

    ``league_fixture`` and ``draft_fixture`` are swappable per test, so one
    harness covers "rosters loaded", "rosters empty" and "draft not started".
    ``status`` forces every response to an HTTP status, which is how the
    error-mapping tests drive ``ESPNAccessDenied`` and friends.

    ``league_payload`` and ``settings_payload`` go further: they answer the
    multi-view league read and the ``mSettings`` read from an in-memory dict
    instead of a file. The recorded fixtures are a snapshot of one league on one
    evening, and some things cannot be recorded from it at all — a roster with
    players on it, a week that is not zero, a draw that is not in team-id order.
    Building those in the test that needs them keeps them beside their
    assertion, and keeps ``tests/fixtures/espn`` a set of files the recorder can
    rewrite without anyone hand-editing the result.
    """

    def __init__(self) -> None:
        self.league_fixture = "roster.json"
        self.league_payload: Any | None = None
        self.settings_payload: Any | None = None
        self.draft_fixture = "draft_detail_partial.json"
        self.status = 200
        self.calls: list[tuple[str, Any]] = []

    def _fixture_for(self, url: str, params: dict[str, Any] | None) -> str:
        view = (params or {}).get("view")
        if isinstance(view, list):
            return self.league_fixture
        if view == "mSettings":
            return "league_settings.json"
        if view == "mDraftDetail":
            return self.draft_fixture
        if view == "players_wl":
            return "pro_players.json"
        if view == "proTeamSchedules_wl":
            return "pro_schedule.json"
        if view == "mPositionalRatings":
            return "positional_ratings.json"
        if view == "kona_player_info":
            return "free_agents.json"
        raise AssertionError(f"fake ESPN has no fixture for {url} view={view!r}")

    def get(self, url: str, params: Any = None, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, params))
        if self.status != 200:
            return _FakeResponse({}, self.status)
        view = (params or {}).get("view")
        if self.league_payload is not None and isinstance(view, list):
            return _FakeResponse(self.league_payload)
        if self.settings_payload is not None and view == "mSettings":
            return _FakeResponse(self.settings_payload)
        return _FakeResponse(load_espn_fixture(self._fixture_for(url, params)))


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the network. Ever.

    The suite has to pass on a box with no internet and no credentials, and a
    test that quietly talks to ESPN is worse than one that fails: it passes
    until it does not. Every HTTP stack in play is stopped at its real
    transport, so an explicit ``httpx.MockTransport`` still works.

    There are three, not two. ``httpx`` is hal-mary's own raw ESPN reads,
    ``requests`` is what the ``espn_api`` library uses, and ``httpx2`` arrives
    with the ``mcp`` SDK. Nothing calls out through the third today; it is
    blocked anyway, because a stack that is unblocked only for as long as nobody
    uses it is a hole with a timer on it.
    """

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a test tried to open a real network connection; mock it with "
            "httpx.MockTransport or the fake_espn fixture"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", explode)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", explode)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", explode)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", explode)
    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", explode)


@pytest.fixture
def fake_espn(monkeypatch: pytest.MonkeyPatch) -> FakeEspnApi:
    """Install the fake in place of ``requests.get`` for the espn_api library."""
    fake = FakeEspnApi()
    monkeypatch.setattr(espn_requests.requests, "get", fake.get)
    return fake


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any library HTTP call an immediate, loud failure."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(espn_requests.requests, "get", explode)


def draft_transport(
    payload: Any = None,
    status: int = 200,
    requests_seen: list[httpx.Request] | None = None,
    raises: Exception | None = None,
) -> httpx.MockTransport:
    """An ``httpx`` transport that answers the raw draftDetail call from a fixture."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests_seen is not None:
            requests_seen.append(request)
        if raises is not None:
            raise raises
        return httpx.Response(status, json=payload if payload is not None else {})

    return httpx.MockTransport(handler)
