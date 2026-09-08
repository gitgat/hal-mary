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

**A pick is not a pick until a player is attached to it.** ESPN pre-populates the
whole draft board before a draft starts, so that raw payload arrives full of
empty slots. :meth:`EspnClient.draft_picks` returns only the slots a player has
actually been drafted into; :meth:`EspnClient.draft_schedule` returns every slot,
because those empty rows are the pick schedule. :func:`pick_is_made` is the one
place that rule is written down.
"""

from __future__ import annotations

import json
import time
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
    "DRAFT_VIEW",
    "LEAGUE_ENDPOINT_TEMPLATE",
    "NAME_MAP_RETRY_COOLDOWN_S",
    "UNMADE_PLAYER_ID",
    "EspnAuthError",
    "EspnClient",
    "EspnError",
    "EspnLeagueNotFound",
    "EspnUnavailable",
    "pick_is_made",
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

#: Free-agent pull size. Big enough to cover a full waiver wire mid-season.
DEFAULT_FREE_AGENT_SIZE = 200

#: What ESPN puts in an unmade slot's ``playerId``.
#:
#: ESPN writes the *entire* draft board before a draft starts: one row per slot,
#: every round, every team. The first real sync of the 2026 league answered with
#: 96 such rows — 6 teams by 16 rounds — all carrying this id and no player.
UNMADE_PLAYER_ID = -1

# How long to leave a failed player-name-map build alone. Rebuilding it is a
# full league fetch, so retrying on every five-second draft poll would hammer an
# ESPN that is already failing; never retrying would let one transient 500 cost
# us every player name for the rest of the draft. This is a backoff, not a
# tunable, and it is bounded below by the poll interval and above by how long a
# draft can tolerate unnamed picks.
NAME_MAP_RETRY_COOLDOWN_S = 30.0


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


def pick_is_made(raw: dict[str, Any]) -> bool:
    """Has a real player been drafted into this raw ``draftDetail.picks`` row?

    **This is the only definition of "a pick happened" in the project.** It lives
    here, at the boundary, so that no consumer downstream has to remember that
    ESPN's board is pre-populated. :mod:`scripts.record_espn_fixtures` imports it
    rather than restating the rule.

    A pick counts only when a player is attached: ``playerId`` present and above
    zero. :data:`UNMADE_PLAYER_ID` is what ESPN really writes, but ``0``, ``None``
    and a missing key are treated the same way — the cost of being wrong is a
    phantom pick that tells the draft loop the draft is further along than it is,
    and no plausible reading of any of those four is "somebody was drafted".

    ``bool`` is excluded explicitly because ``True`` is an ``int`` greater than
    zero in Python and would otherwise sail through as player id 1.
    """
    player_id = raw.get("playerId")
    return isinstance(player_id, int) and not isinstance(player_id, bool) and player_id > 0


def _flag(value: Any) -> bool | None:
    """One of ESPN's own booleans, or ``None`` when it said nothing.

    Absent and false are different: "ESPN did not tell us" must not read as
    "ESPN said no" in a log line someone is using to work out what happened.
    """
    return value if isinstance(value, bool) else None


def _overall_pick_order(raw: dict[str, Any]) -> tuple[bool, int]:
    """Sort key that puts a row with no ``overallPickNumber`` last without raising."""
    overall = raw.get("overallPickNumber")
    if isinstance(overall, int) and not isinstance(overall, bool):
        return (False, overall)
    return (True, 0)


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
        # A hung request during a draft is worse than a failed one: the pick
        # clock is 90 seconds and the poll interval is 5, so a request
        # that never returns silently stops the loop. The bounds come from
        # config.toml's [espn] section, next door to draft.poll_seconds.
        self._timeout = timeout or httpx.Timeout(
            settings.espn.read_timeout_s,
            connect=settings.espn.connect_timeout_s,
            read=settings.espn.read_timeout_s,
        )
        self._league: Any | None = None
        self._request_layer: Any | None = None
        self._raw_settings: dict[str, Any] | None = None
        self._name_map: dict[int, str] | None = None
        # monotonic deadline before which a failed name-map build is not retried
        self._name_map_retry_after = 0.0
        # What the last mDraftDetail read said about the draft itself. Recorded
        # off that read rather than fetched, so draft_status() costs nothing.
        self._draft_status: dict[str, Any] | None = None

    # -- plumbing ----------------------------------------------------------

    @property
    def timeout(self) -> httpx.Timeout:
        """The transport limits in force, for whoever needs to report them."""
        return self._timeout

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

    def current_week(self) -> int | None:
        """Which NFL week ESPN thinks it is, or ``None`` if it will not say.

        Separate from :meth:`league_settings` on purpose. That method answers
        from the small ``mSettings`` response; this one needs the League the
        library builds, and the two are kept apart so a caller wanting only the
        league's shape does not pay for the heavier read. During a sync the
        League is built anyway, so this costs nothing there.

        The library clamps its own ``current_week`` to the final scoring period,
        which is what makes it right in January: the season is over, the roster
        still exists, and "week 19" would put every player on a bye.

        This is the number the whole bye-week check is measured against, so it is
        read rather than derived. A week worked out from the calendar looks right
        every year until the season it does not, and a bye warning against the
        wrong week either flags healthy players or — much worse — flags nobody.
        So ``None``, not a guess: every caller handles it, and the in-season jobs
        fall back to the week the model established from the live NFL schedule.

        A box with no cookies at all is the one case that still raises. That is a
        configuration problem the status page has to report, not a week ESPN
        declined to give, and every other read on this client raises for it too.
        """
        self._require_config()
        try:
            week = getattr(self._library(), "current_week", None)
        except EspnError:
            # Expired cookies or an ESPN outage. The caller falls back; every
            # other read on this client raises for the caller that needs it to.
            return None
        try:
            number = int(week)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

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
        briefly unavailable would be a much worse failure.

        A failure is **not** cached. The draft loop holds one client for the
        whole draft, so caching an empty map after one transient 500 would mean
        every pick from then on rendering as "player 12345" — the failure
        outliving its cause by three hours. Instead the failure is held off for
        :data:`NAME_MAP_RETRY_COOLDOWN_S`, which is long enough that a five-
        second poll does not hammer an ESPN that is already struggling, and
        short enough that names come back within a pick or two of ESPN
        recovering.

        **The cooldown is per-instance, and so is the cache it protects.** Both
        only work because the draft loop holds one long-lived client for the
        whole draft. A caller that builds a fresh ``EspnClient`` on every poll
        gets neither: it rebuilds the name map from scratch every five seconds
        when ESPN is healthy, and retries a failing ESPN just as often.
        """
        if self._name_map is None and time.monotonic() < self._name_map_retry_after:
            return {}
        try:
            return self.player_name_map()
        except EspnError:
            self._name_map_retry_after = time.monotonic() + NAME_MAP_RETRY_COOLDOWN_S
            return {}

    # -- draft -------------------------------------------------------------

    def _raw_draft_rows(self) -> list[dict[str, Any]]:
        """``draftDetail.picks`` from the raw endpoint, one fresh GET per call.

        **``draftDetail.drafted`` is ignored.** The library gates on that flag,
        which may not be set until the draft is over — by which time the answer
        is useless.
        """
        payload = self._get(DRAFT_VIEW)
        detail = payload.get("draftDetail", {}) or {}
        rows = detail.get("picks") or []
        rows = [row for row in rows if isinstance(row, dict)]
        rows.sort(key=_overall_pick_order)
        self._draft_status = {
            "in_progress": _flag(detail.get("inProgress")),
            "drafted": _flag(detail.get("drafted")),
            "slots": len(rows),
            "picks_made": sum(1 for row in rows if pick_is_made(row)),
            "read_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        return rows

    def draft_status(self) -> dict[str, Any] | None:
        """What the last draft read said about the draft itself, or ``None``.

        **This makes no request.** It is recorded by :meth:`_raw_draft_rows`, so
        the draft loop — which calls :meth:`draft_picks` every poll anyway — can
        ask how far along the draft is without a second GET on the pick clock.
        ``None`` means nothing has been read yet, which is the honest answer and
        not the same as "no draft".

        Four fields, and the two that decide anything are the counts:

        ``slots``
            how many rows ESPN's board has. It pre-populates every slot of the
            draft, so this is 96 for a 6-team, 16-round league from the day the
            league exists.
        ``picks_made``
            how many of those rows have a real player attached
            (:func:`pick_is_made`).
        ``in_progress`` / ``drafted``
            ``draftDetail``'s own booleans, reported for corroboration and for
            the log. **Neither is trustworthy on its own** — see
            :func:`hal_mary.draft.loop.draft_phase`, which is where the rule
            lives — so they are carried, not obeyed.
        """
        return dict(self._draft_status) if self._draft_status is not None else None

    def draft_picks(self) -> list[dict[str, Any]]:
        """Picks that have actually happened, sorted by overall pick number.

        **ESPN pre-populates the whole draft board before the draft starts**, so
        ``draftDetail.picks`` is 96 rows for a 6-team, 16-round league from the
        moment the league exists — every one an empty slot carrying
        :data:`UNMADE_PLAYER_ID`. Those rows are filtered out here, at the
        boundary, by :func:`pick_is_made`: a pick is a pick only once a real
        player is attached to it. Filtering at this layer is deliberate — it is
        the one place that knows ESPN's vocabulary, so nothing downstream has to
        remember the rule. Before the draft this returns ``[]``, which is the
        truthful answer and the one the draft loop is built on.

        The empty slots are not thrown away: they are the pick schedule, and
        :meth:`draft_schedule` returns them.
        """
        made = [row for row in self._raw_draft_rows() if pick_is_made(row)]
        # Naming nobody costs a full league fetch, and the draft loop polls this
        # every five seconds through however long the board sits empty.
        names = self._names_or_empty() if made else {}
        return [
            {
                "overall_pick": raw.get("overallPickNumber"),
                "round_num": raw.get("roundId"),
                "round_pick": raw.get("roundPickNumber"),
                "team_id": raw.get("teamId"),
                "player_id": raw["playerId"],
                "player_name": names.get(raw["playerId"]),
            }
            for raw in made
        ]

    def draft_schedule(self) -> list[dict[str, Any]]:
        """Every slot on the draft board, filled or not, by overall pick number.

        The rows :meth:`draft_picks` filters out carry real information: which
        team owns which overall pick, and how many rounds the draft runs. That is
        exactly what the pick countdown needs, and reading it from ESPN beats
        deriving it from a pick order and a snake rule we would have to keep in
        step with the league's settings.

        Each slot is ``{"overall_pick", "round_num", "round_pick", "team_id",
        "made"}``. Returns ``[]`` when ESPN has not built a board yet.

        **The ownership is only as current as ESPN's draft order.** This league's
        ``draftSettings.orderType`` is ``DRAFT_START``, meaning the order is
        assigned when the draft begins; the board ESPN pre-populates is derived
        from the provisional order, so ``team_id`` here can change the moment the
        draft opens. Re-read it then rather than caching it from a pre-draft sync.
        """
        return [
            {
                "overall_pick": raw.get("overallPickNumber"),
                "round_num": raw.get("roundId"),
                "round_pick": raw.get("roundPickNumber"),
                "team_id": raw.get("teamId"),
                "made": pick_is_made(raw),
            }
            for raw in self._raw_draft_rows()
        ]

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
