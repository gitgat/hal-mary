"""A read-only client for one ESPN fantasy football league.

Every public method returns **plain dicts and lists**. Nothing downstream — not
the persistence layer, not the board, not the draft loop, not a test — is
allowed to depend on an ``espn_api`` type, because those types change with the
library and hold live HTTP handles.

Two rules shape the implementation:

**The library is constructed lazily.** ``EspnClient(settings)`` on a box with no
cookies must not raise and must not reach the network: ``hal-mary --help`` and
the whole test suite would otherwise depend on ESPN being up.

**Draft picks bypass the library.** ``espn_api``'s ``_fetch_draft`` returns
early unless ``draftDetail.drafted`` is true, and ``refresh_draft()`` appends to
a list it never clears. :meth:`EspnClient.draft_picks` reads
``draftDetail.picks`` from the raw endpoint and ignores the ``drafted`` flag
entirely.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from espn_api.football.constant import POSITION_MAP
from espn_api.requests.espn_requests import (
    ESPNAccessDenied,
    EspnFantasyRequests,
    ESPNInvalidLeague,
    ESPNUnknownError,
)

from hal_mary.config import Settings

__all__ = [
    "CONNECT_TIMEOUT_S",
    "DRAFT_VIEW",
    "LEAGUE_ENDPOINT_TEMPLATE",
    "READ_TIMEOUT_S",
    "EspnAuthError",
    "EspnClient",
    "EspnError",
    "EspnLeagueNotFound",
    "EspnUnavailable",
]

#: The read-only ESPN fantasy host. Same base the library uses.
LEAGUE_ENDPOINT_TEMPLATE = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
    "/seasons/{season}/segments/0/leagues/{league_id}"
)

#: The view that carries ``draftDetail.picks``.
DRAFT_VIEW = "mDraftDetail"

#: The cheapest authenticated view; used by :meth:`EspnClient.check_auth`.
SETTINGS_VIEW = "mSettings"

# A hung request during a draft is worse than a failed one: the pick clock is 60
# to 90 seconds and the poll interval is 5, so a request that never returns
# silently stops the loop. These are transport limits, not tunables — they are
# bounded by the draft clock, not by taste — so they live here rather than in
# config.toml, and the constructor takes an override for the one caller (a test)
# that needs a different value.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 15.0

#: Free-agent pull size. Big enough to cover a full waiver wire mid-season.
DEFAULT_FREE_AGENT_SIZE = 200


class EspnError(RuntimeError):
    """Base for every failure this module reports.

    No ``requests`` or ``httpx`` exception is allowed to escape the package: a
    caller catching ``EspnError`` must be catching everything.
    """


class EspnAuthError(EspnError):
    """ESPN rejected the cookies (HTTP 401/403), or there are none to send."""


class EspnLeagueNotFound(EspnError):
    """ESPN has no such league for that season (HTTP 404)."""


class EspnUnavailable(EspnError):
    """ESPN is down, slow, or unreachable (HTTP 5xx, timeout, DNS, TLS)."""


def _build_league(settings: Settings) -> Any:
    """Construct the ``espn_api`` League.

    Imported inside the function so ``import hal_mary.espn`` stays cheap, and
    named at module level so tests can replace it.
    """
    from espn_api.football import League

    return League(
        league_id=settings.league_id,
        year=settings.season,
        espn_s2=settings.espn_s2,
        swid=settings.swid,
    )


def _clean(value: Any) -> Any:
    """Normalise ESPN's several ways of saying "absent" to ``None``.

    ``espn_api``'s ``json_parsing`` returns ``[]`` when a key is missing, so a
    player with no injury status arrives as an empty list rather than ``None``.
    """
    if value in (None, "", [], {}):
        return None
    return value


def _epoch_ms_to_iso(value: Any) -> str | None:
    """ESPN dates are epoch milliseconds; we store ISO-8601 UTC."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="seconds")


def _owner_name(owners: list[Any]) -> str | None:
    """A human name for a team's owner(s).

    ESPN gives members as dicts; some leagues have co-owners and some members
    have only a display name.
    """
    names = []
    for owner in owners or []:
        if not isinstance(owner, dict):
            continue
        full = " ".join(
            part for part in (owner.get("firstName"), owner.get("lastName")) if part
        ).strip()
        name = full or owner.get("displayName")
        if name:
            names.append(name)
    return ", ".join(names) or None


class EspnClient:
    """Reads one league. Never writes to ESPN.

    ``transport`` and ``timeout`` exist for tests: an ``httpx.MockTransport``
    keeps the suite off the network without monkeypatching this module.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._timeout = timeout or httpx.Timeout(
            READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S
        )
        self._league: Any | None = None
        self._request_layer: Any | None = None
        self._raw_settings: dict[str, Any] | None = None
        self._name_map: dict[int, str] | None = None

    # -- plumbing ----------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return LEAGUE_ENDPOINT_TEMPLATE.format(
            season=self.settings.season, league_id=self.settings.league_id
        )

    def _require_config(self) -> None:
        """Fail before building a URL, or a request, out of ``None``.

        All four keys are reported together because a box that is missing one is
        usually missing several, and the useful message is the whole list.
        Without LEAGUE_ID the URL would be ``/seasons/None/leagues/None``, which
        ESPN answers with a 404 — reported as "league not found", which sends
        whoever is reading the status page looking in exactly the wrong place.
        """
        missing = [
            name
            for name, value in (
                ("ESPN_S2", self.settings.espn_s2),
                ("SWID", self.settings.swid),
                ("LEAGUE_ID", self.settings.league_id),
                ("SEASON", self.settings.season),
            )
            if not value
        ]
        if missing:
            raise EspnAuthError(
                f"{', '.join(missing)} not set; copy the cookies from a logged-in browser "
                "session into .env (see .env.example)"
            )

    def _cookies(self) -> dict[str, str]:
        self._require_config()
        return {"espn_s2": self.settings.espn_s2, "SWID": self.settings.swid}

    def _raise_for_status(self, status: int) -> None:
        if status in (401, 403):
            raise EspnAuthError(
                f"ESPN rejected the request (HTTP {status}): the ESPN_S2/SWID cookies have "
                "expired or do not grant access to this league"
            )
        if status == 404:
            raise EspnLeagueNotFound(
                f"ESPN has no league {self.settings.league_id} for season "
                f"{self.settings.season} (HTTP 404)"
            )
        if status >= 500:
            raise EspnUnavailable(f"ESPN returned HTTP {status}")
        if status != 200:
            raise EspnUnavailable(f"ESPN returned an unexpected HTTP {status}")

    def _get(self, view: str) -> Any:
        """One raw, cookie-authenticated GET against the league endpoint."""
        cookies = self._cookies()
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport, cookies=cookies
            ) as http:
                response = http.get(self.endpoint, params={"view": view})
        except httpx.HTTPError as exc:
            raise EspnUnavailable(f"ESPN is unreachable: {exc}") from exc

        self._raise_for_status(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise EspnUnavailable(f"ESPN returned a body that is not JSON: {exc}") from exc
        # The leagueHistory form of the endpoint answers with a single-element list.
        return payload[0] if isinstance(payload, list) and payload else payload

    def _library(self) -> Any:
        """The lazily-built ``espn_api`` League, with its exceptions wrapped."""
        if self._league is None:
            self._require_config()
            try:
                self._league = _build_league(self.settings)
            except ESPNAccessDenied as exc:
                raise EspnAuthError(f"ESPN rejected the cookies: {exc}") from exc
            except ESPNInvalidLeague as exc:
                raise EspnLeagueNotFound(str(exc)) from exc
            except ESPNUnknownError as exc:
                raise EspnUnavailable(str(exc)) from exc
            except Exception as exc:  # requests' own errors, and anything else
                raise EspnUnavailable(f"ESPN read failed: {exc}") from exc
        return self._league

    def _library_call(self, call, *args, **kwargs):
        """Run one library method, wrapping its exceptions into ours."""
        try:
            return call(*args, **kwargs)
        except ESPNAccessDenied as exc:
            raise EspnAuthError(f"ESPN rejected the cookies: {exc}") from exc
        except ESPNInvalidLeague as exc:
            raise EspnLeagueNotFound(str(exc)) from exc
        except ESPNUnknownError as exc:
            raise EspnUnavailable(str(exc)) from exc
        except EspnError:
            raise
        except Exception as exc:
            raise EspnUnavailable(f"ESPN read failed: {exc}") from exc

    def _requests(self) -> Any:
        """The library's request layer, without the League's heavy first fetch.

        ``mSettings`` is a small response and building a whole ``League`` to get
        at it would pull the player list, every roster and the schedule.
        """
        if self._request_layer is None:
            self._require_config()
            self._request_layer = EspnFantasyRequests(
                sport="nfl",
                year=self.settings.season,
                league_id=self.settings.league_id,
                cookies=self._cookies(),
            )
        return self._request_layer

    def _settings_payload(self) -> dict[str, Any]:
        """The raw ``mSettings`` response, fetched once per client.

        The library keeps a parsed ``Settings`` object but throws the raw dict
        away, and the raw dict is where ``draftSettings`` and ``rosterSettings``
        live — the draft date, the draft order and the lineup slot counts. One
        extra small request per sync is a fair price for not re-deriving ESPN's
        slot ids ourselves.
        """
        if self._raw_settings is None:
            self._raw_settings = self._library_call(
                self._requests().league_get, params={"view": SETTINGS_VIEW}
            )
        return self._raw_settings

    # -- league ------------------------------------------------------------

    def league_settings(self) -> dict[str, Any]:
        """League name, size, scoring, roster slots and draft schedule."""
        payload = self._settings_payload()
        raw = payload.get("settings", {}) or {}
        draft = raw.get("draftSettings", {}) or {}
        scoring = raw.get("scoringSettings", {}) or {}
        lineup_counts = (raw.get("rosterSettings", {}) or {}).get("lineupSlotCounts", {}) or {}

        roster_slots: dict[str, int] = {}
        for slot_id, count in lineup_counts.items():
            if not count:
                continue
            name = POSITION_MAP.get(int(slot_id), str(slot_id))
            roster_slots[name] = roster_slots.get(name, 0) + int(count)

        return {
            "season": payload.get("seasonId", self.settings.season),
            "league_id": payload.get("id", self.settings.league_id),
            "name": raw.get("name"),
            "team_count": raw.get("size"),
            "scoring_type": scoring.get("scoringType"),
            "draft_type": draft.get("type"),
            "draft_date": _epoch_ms_to_iso(draft.get("date")),
            "roster_slots": roster_slots,
            "raw_json": json.dumps(raw, sort_keys=True),
        }

    def _draft_slots(self) -> dict[int, int]:
        """team_id -> 1-based draft slot, from ``draftSettings.pickOrder``."""
        order = (self._settings_payload().get("settings", {}) or {}).get(
            "draftSettings", {}
        ).get("pickOrder") or []
        return {int(team_id): index + 1 for index, team_id in enumerate(order)}

    def teams(self) -> list[dict[str, Any]]:
        """Every team in the league, sorted by team id."""
        league = self._library()
        slots = self._draft_slots()
        return [
            {
                "team_id": team.team_id,
                "name": team.team_name,
                "owner": _owner_name(getattr(team, "owners", [])),
                "abbrev": team.team_abbrev,
                "draft_slot": slots.get(team.team_id),
            }
            for team in league.teams
        ]

    def rosters(self) -> list[dict[str, Any]]:
        """One row per rostered player, across every team."""
        league = self._library()
        rows: list[dict[str, Any]] = []
        for team in league.teams:
            for player in team.roster:
                rows.append(
                    {
                        "team_id": team.team_id,
                        "player_id": player.playerId,
                        "name": player.name,
                        "position": _clean(player.position),
                        "pro_team": _clean(player.proTeam),
                        "injury_status": _clean(player.injuryStatus),
                        "slot": _clean(player.lineupSlot),
                    }
                )
        return rows

    def free_agents(
        self, size: int = DEFAULT_FREE_AGENT_SIZE, position: str | None = None
    ) -> list[dict[str, Any]]:
        """Available players, most-owned first (ESPN's own ordering)."""
        league = self._library()
        players = self._library_call(league.free_agents, size=size, position=position)
        return [
            {
                "player_id": player.playerId,
                "name": player.name,
                "position": _clean(player.position),
                "pro_team": _clean(player.proTeam),
                "injury_status": _clean(player.injuryStatus),
                "percent_owned": player.percent_owned,
            }
            for player in players
        ]

    # -- players -----------------------------------------------------------

    def player_name_map(self, refresh: bool = False) -> dict[int, str]:
        """player_id -> name, so a pick can be labelled the moment it lands.

        Built from ESPN's active-player list (which the library fetches when it
        builds the League) plus the current roster and free-agent pulls, so a
        pick can be named even for a player hal-mary has never synced. Cached:
        the draft loop calls this through :meth:`draft_picks` every five
        seconds and must not re-fetch the league each time.
        """
        if self._name_map is not None and not refresh:
            return self._name_map

        league = self._library()
        names: dict[int, str] = {
            key: value
            for key, value in getattr(league, "player_map", {}).items()
            if isinstance(key, int) and isinstance(value, str)
        }
        for row in self.rosters():
            if row["player_id"] is not None and row["name"]:
                names[int(row["player_id"])] = row["name"]
        for row in self.free_agents():
            if row["player_id"] is not None and row["name"]:
                names[int(row["player_id"])] = row["name"]

        self._name_map = names
        return names

    def _names_or_empty(self) -> dict[int, str]:
        """Names, but never at the cost of a pick.

        A pick whose player we cannot name is shown as "player 12345" and the
        draft carries on; a draft loop that died because the player list was
        briefly unavailable would be a much worse failure. The empty result is
        cached so a broken league read is not retried on every five-second poll.
        """
        try:
            return self.player_name_map()
        except EspnError:
            self._name_map = {}
            return self._name_map

    # -- draft -------------------------------------------------------------

    def draft_picks(self) -> list[dict[str, Any]]:
        """Every pick ESPN has recorded, sorted by overall pick number.

        Reads ``draftDetail.picks`` straight from the raw endpoint and **ignores
        ``draftDetail.drafted``**. The library gates on that flag, which may not
        be set until the draft is over — by which time the answer is useless.
        """
        payload = self._get(DRAFT_VIEW)
        raw_picks = (payload.get("draftDetail", {}) or {}).get("picks") or []
        names = self._names_or_empty() if raw_picks else {}

        picks: list[dict[str, Any]] = []
        for raw in raw_picks:
            player_id = raw.get("playerId")
            picks.append(
                {
                    "overall_pick": raw.get("overallPickNumber"),
                    "round_num": raw.get("roundId"),
                    "round_pick": raw.get("roundPickNumber"),
                    "team_id": raw.get("teamId"),
                    "player_id": player_id,
                    "player_name": names.get(player_id) if player_id is not None else None,
                }
            )
        picks.sort(key=lambda pick: (pick["overall_pick"] is None, pick["overall_pick"]))
        return picks

    # -- health ------------------------------------------------------------

    def check_auth(self) -> tuple[bool, str]:
        """Are the cookies still good? Returns ``(ok, human-readable reason)``.

        The status page calls this hourly and a monitoring script calls it via
        ``hal-mary espn-check``, so the failure modes stay distinct: expired
        cookies need Caroline's browser, a missing league needs the config
        checked, and an ESPN outage needs nothing but patience.
        """
        try:
            self._get(SETTINGS_VIEW)
        except EspnAuthError as exc:
            if "not set" in str(exc):
                return False, str(exc)
            return False, f"ESPN credentials have expired: {exc}"
        except EspnLeagueNotFound as exc:
            return False, f"League not found: {exc}"
        except EspnUnavailable as exc:
            message = str(exc)
            if "unreachable" in message:
                return False, f"ESPN is unreachable: {message}"
            return False, f"ESPN is unavailable: {message}"
        return True, (
            f"ESPN credentials are valid for league {self.settings.league_id}, "
            f"season {self.settings.season}"
        )
