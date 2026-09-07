#!/usr/bin/env python
"""Re-record ``tests/fixtures/espn/*.json`` from the real league, in one command.

    uv run python scripts/record_espn_fixtures.py

The fixtures committed today are **synthetic**: hand-built to the shapes in the
``espn_api`` source because there were no credentials when the ESPN client was
written. They are good enough to pin the mapping and to keep the library honest,
and they are not good enough to trust about ESPN's real vocabulary. Run this the
first time cookies exist, read the diff, and commit it.

Credentials come from the environment (or ``.env``) via ``hal_mary.config`` and
are **never written to a file**. Every payload goes through :func:`scrub` first,
which removes the live cookie values, redacts anything under a credential-shaped
key, and replaces SWIDs, member names and team names with stable pseudonyms — so
the fixtures still hang together without carrying a single real person's name.

One thing it deliberately keeps is the **league's own name**, which is far more
useful in a fixture than `Team 1` and is not personal data in the way a member's
name is. Read the diff; if the league is named after somebody, change it by hand.

The script is deliberately not a package module: it is an operator tool, run by
hand, and nothing in ``src/`` imports it. Its scrubber is covered by
``tests/unit/test_espn_fixture_recorder.py``, because it is the one piece of
code here whose job is to take live credentials and write them towards git.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "espn"

REDACTED = "<scrubbed>"

#: A value under a key matching any of these is redacted whatever it looks like.
SECRET_KEY_RE = re.compile(
    r"espn_?s2|swid|cookie|token|auth|secret|password|passwd|session|credential|e?mail",
    re.IGNORECASE,
)

#: Keys holding a real person's name. ESPN names every member of the league, and
#: a fixture recorded verbatim would put Caroline's leaguemates into a public git
#: history permanently. Pseudonymised rather than redacted so the same person
#: still reads as the same person across fields.
PERSON_NAME_KEYS = frozenset({"firstName", "lastName", "displayName", "nickName"})

#: Keys holding a team's name. People name fantasy teams after themselves more
#: often than not, so these are personal data too. ``location`` and ``nickname``
#: are pseudonymised wherever they appear — over-scrubbing an NFL team's city
#: costs nothing, because the library resolves pro teams by id.
TEAM_NAME_KEYS = frozenset({"location", "nickname"})

#: A bare ``name`` is only a team name when it sits on a team-shaped object. The
#: league's own name and the division names are not personal data and are much
#: more useful in a fixture kept intact.
TEAM_MARKER_KEYS = frozenset({"owners", "playoffSeed", "roster"})

#: ESPN member ids are SWIDs: a UUID in braces. They are personal identifiers,
#: so they are pseudonymised rather than dropped — the fixtures need the teams
#: and their owners to still refer to each other.
SWID_RE = re.compile(r"^\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?$")

#: Anything long, opaque and unbroken is assumed to be a credential. ESPN_S2 is
#: a ~300 character percent-encoded blob; no legitimate field in these payloads
#: looks like that.
OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9%+/=_.\-]{64,}$")


# --- scrubbing -------------------------------------------------------------


class _Pseudonyms:
    """Stable fakes, assigned in first-seen order.

    Stability is the point. Blanking every SWID would collapse the team-to-owner
    links and the fixture would stop meaning anything; blanking every name would
    make four teams indistinguishable. Each distinct real value gets its own fake
    and keeps it everywhere it appears.
    """

    def __init__(self, template: str) -> None:
        self._template = template
        self._seen: dict[str, str] = {}

    def for_value(self, value: str) -> str:
        if value not in self._seen:
            self._seen[value] = self._template.format(n=len(self._seen) + 1)
        return self._seen[value]


def scrub(payload: Any, secrets: list[str] | None = None) -> Any:
    """Return ``payload`` with every credential-shaped thing removed.

    ``secrets`` are the live values read from the environment. Passing them is
    belt and braces over the pattern matching below: an exact substring match is
    the only check that cannot be fooled by ESPN inventing a new field name.
    """
    live = [value for value in (secrets or []) if value]
    swids = _Pseudonyms("{{00000000-0000-0000-0000-{n:012d}}}")
    people = _Pseudonyms("Person {n}")
    team_names = _Pseudonyms("Team {n}")

    def scrub_string(value: str, key: str | None) -> str:
        if key and SECRET_KEY_RE.search(key):
            return REDACTED
        if any(secret and secret in value for secret in live):
            return REDACTED
        if SWID_RE.match(value):
            return swids.for_value(value)
        if OPAQUE_TOKEN_RE.match(value):
            return REDACTED
        return value

    def scrub_field(name: str, value: Any, is_team: bool) -> Any:
        if SECRET_KEY_RE.search(name):
            return REDACTED
        if isinstance(value, str) and value:
            if name in PERSON_NAME_KEYS:
                return people.for_value(value)
            if name in TEAM_NAME_KEYS or (name == "name" and is_team):
                return team_names.for_value(value)
        return walk(value, name)

    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            is_team = bool(TEAM_MARKER_KEYS & node.keys())
            return {
                name: scrub_field(str(name), value, is_team) for name, value in node.items()
            }
        if isinstance(node, list):
            return [walk(item, key) for item in node]
        if isinstance(node, str):
            return scrub_string(node, key)
        return node

    return walk(payload)


# --- what to record --------------------------------------------------------


@dataclass(frozen=True)
class FixtureSpec:
    """One ESPN request, and where its answer belongs."""

    key: str
    filename: str
    base: str  # "league" or "season"
    path: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


LEAGUE_VIEWS = ["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"]

#: Free-agent and player filters, copied from what ``espn_api`` sends so the
#: recorded payloads match what the library will actually receive.
FREE_AGENT_FILTER = {
    "players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "filterSlotIds": {"value": []},
        "limit": 200,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
    }
}
ACTIVE_PLAYER_FILTER = {"filterActive": {"value": True}}

SPECS = (
    FixtureSpec("mSettings", "league_settings.json", "league", params={"view": "mSettings"}),
    # One response feeds two fixtures: roster.json keeps it whole, teams.json is
    # the same league with empty rosters — which is also what ESPN really
    # returns before the draft.
    FixtureSpec("league", "roster.json", "league", params={"view": LEAGUE_VIEWS}),
    FixtureSpec(
        "kona_player_info",
        "free_agents.json",
        "league",
        params={"view": "kona_player_info"},
        headers={"x-fantasy-filter": json.dumps(FREE_AGENT_FILTER)},
    ),
    FixtureSpec(
        "players_wl",
        "pro_players.json",
        "season",
        path="/players",
        params={"view": "players_wl"},
        headers={"x-fantasy-filter": json.dumps(ACTIVE_PLAYER_FILTER)},
    ),
    FixtureSpec(
        "proTeamSchedules_wl",
        "pro_schedule.json",
        "season",
        params={"view": "proTeamSchedules_wl"},
    ),
    FixtureSpec(
        "mPositionalRatings",
        "positional_ratings.json",
        "league",
        params={"view": "mPositionalRatings"},
    ),
    FixtureSpec("mDraftDetail", "draft_detail.json", "league", params={"view": "mDraftDetail"}),
)

DRAFT_FILENAMES = (
    "draft_detail_empty.json",
    "draft_detail_partial.json",
    "draft_detail_full.json",
)

#: ESPN's active-player list runs to thousands of entries and only a handful of
#: fields matter to us. Recording all of it would put a megabyte of noise in git.
PRO_PLAYER_FIELDS = ("id", "fullName", "defaultPositionId", "proTeamId", "eligibleSlots")
PRO_PLAYER_LIMIT = 250


def recordable_filenames() -> set[str]:
    """Every fixture filename this script can produce."""
    names = {spec.filename for spec in SPECS if spec.key != "mDraftDetail"}
    names.add("teams.json")
    return names | set(DRAFT_FILENAMES)


# --- fetching --------------------------------------------------------------


def build_fetcher(settings: Any) -> Callable[[FixtureSpec], Any]:
    """A fetcher that talks to ESPN with the configured cookies."""
    import httpx

    league_base = (
        "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
        f"/seasons/{settings.season}/segments/0/leagues/{settings.league_id}"
    )
    season_base = (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{settings.season}"
    )
    cookies = {"espn_s2": settings.espn_s2, "SWID": settings.swid}
    timeout = httpx.Timeout(
        settings.espn.read_timeout_s,
        connect=settings.espn.connect_timeout_s,
        read=settings.espn.read_timeout_s,
    )

    def fetch(spec: FixtureSpec) -> Any:
        base = league_base if spec.base == "league" else season_base
        with httpx.Client(timeout=timeout, cookies=cookies) as http:
            response = http.get(base + spec.path, params=spec.params, headers=spec.headers)
        response.raise_for_status()
        payload = response.json()
        return payload[0] if isinstance(payload, list) and spec.base == "league" else payload

    return fetch


# --- recording -------------------------------------------------------------


def _reduce_pro_players(players: Any, keep_ids: set[int]) -> Any:
    """Keep the players the other fixtures mention, then fill up to the limit."""
    if not isinstance(players, list):
        return players
    reduced = [
        {field_name: player.get(field_name) for field_name in PRO_PLAYER_FIELDS}
        for player in players
        if isinstance(player, dict)
    ]
    wanted = [player for player in reduced if player.get("id") in keep_ids]
    rest = [player for player in reduced if player.get("id") not in keep_ids]
    return (wanted + rest)[: max(PRO_PLAYER_LIMIT, len(wanted))]


def _strip_rosters(league: Any) -> Any:
    stripped = json.loads(json.dumps(league))
    for team in stripped.get("teams", []) or []:
        if isinstance(team, dict) and isinstance(team.get("roster"), dict):
            team["roster"]["entries"] = []
    return stripped


def _draft_filename(draft_detail: dict[str, Any]) -> str:
    picks = draft_detail.get("picks") or []
    if not picks:
        return "draft_detail_empty.json"
    return "draft_detail_full.json" if draft_detail.get("drafted") else "draft_detail_partial.json"


def _player_ids(*payloads: Any) -> set[int]:
    """Every player id mentioned anywhere in the given payloads."""
    found: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("playerId", "id") and isinstance(value, int):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for payload in payloads:
        walk(payload)
    return found


def _write(out_dir: Path, filename: str, payload: Any, secrets: list[str] | None) -> Path:
    path = out_dir / filename
    path.write_text(json.dumps(scrub(payload, secrets), indent=2) + "\n", encoding="utf-8")
    return path


def record(
    fetch: Callable[[FixtureSpec], Any],
    out_dir: Path = DEFAULT_OUT_DIR,
    secrets: list[str] | None = None,
    specs: tuple[FixtureSpec, ...] = SPECS,
) -> list[Path]:
    """Fetch every fixture, scrub it, write it. Returns the files written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payloads = {spec.key: fetch(spec) for spec in specs}
    league = payloads["league"]
    draft = payloads["mDraftDetail"]

    keep_ids = _player_ids(league, draft, payloads["kona_player_info"])
    payloads["players_wl"] = _reduce_pro_players(payloads["players_wl"], keep_ids)

    written = [
        _write(out_dir, "league_settings.json", payloads["mSettings"], secrets),
        _write(out_dir, "roster.json", league, secrets),
        _write(out_dir, "teams.json", _strip_rosters(league), secrets),
        _write(out_dir, "free_agents.json", payloads["kona_player_info"], secrets),
        _write(out_dir, "pro_players.json", payloads["players_wl"], secrets),
        _write(out_dir, "pro_schedule.json", payloads["proTeamSchedules_wl"], secrets),
        _write(out_dir, "positional_ratings.json", payloads["mPositionalRatings"], secrets),
    ]

    draft_detail = (draft or {}).get("draftDetail", {}) or {}
    written.append(_write(out_dir, _draft_filename(draft_detail), draft, secrets))
    return written


def _scoped_for_week(specs: tuple[FixtureSpec, ...], week: int) -> tuple[FixtureSpec, ...]:
    """Two views are scoped to a scoring period; the rest ignore it."""
    scoped_keys = ("kona_player_info", "mPositionalRatings")
    return tuple(
        FixtureSpec(
            spec.key,
            spec.filename,
            spec.base,
            spec.path,
            {**spec.params, "scoringPeriodId": week} if spec.key in scoped_keys else spec.params,
            spec.headers,
        )
        for spec in specs
    )


def main() -> int:
    from hal_mary.config import load_settings

    settings = load_settings()
    required = ("ESPN_S2", "SWID", "LEAGUE_ID", "SEASON")
    missing = [key for key in settings.missing_secrets() if key in required]
    if missing:
        print(f"cannot record: {', '.join(missing)} not set (see .env.example)", file=sys.stderr)
        return 2

    fetch = build_fetcher(settings)
    # The scoring-period-scoped views need a week, and ESPN's own current one is
    # in the league payload — so ask for that first and scope the rest to it.
    league_spec = next(spec for spec in SPECS if spec.key == "league")
    week = (fetch(league_spec) or {}).get("scoringPeriodId", 1)

    written = record(
        fetch,
        secrets=[settings.espn_s2 or "", settings.swid or ""],
        specs=_scoped_for_week(SPECS, week),
    )
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    print(
        "\nRead the diff before committing. Only one of the three draft_detail_*.json "
        "files is refreshed per run — the one matching the draft's current state.\n"
        "Member and team names are pseudonymised; the league's own name is not, so "
        "check it is not somebody's."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
