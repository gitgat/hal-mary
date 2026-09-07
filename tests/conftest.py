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

Every fixture under ``tests/fixtures/espn`` is **synthetic**: hand-built to the
shapes in the ``espn_api`` source, not recorded from a real league. Re-record
them with ``scripts/record_espn_fixtures.py`` once credentials exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from espn_api.requests import espn_requests

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "espn"

#: The league the fixtures describe.
FIXTURE_LEAGUE_ID = 1234567
FIXTURE_SEASON = 2025
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
    """

    def __init__(self) -> None:
        self.league_fixture = "roster.json"
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
        return _FakeResponse(load_espn_fixture(self._fixture_for(url, params)))


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the network. Ever.

    The suite has to pass on a box with no internet and no credentials, and a
    test that quietly talks to ESPN is worse than one that fails: it passes
    until it does not. Both HTTP stacks in play are stopped at their real
    transport, so an explicit ``httpx.MockTransport`` still works.
    """

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a test tried to open a real network connection; mock it with "
            "httpx.MockTransport or the fake_espn fixture"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", explode)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", explode)
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
