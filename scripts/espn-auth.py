#!/usr/bin/env python3
"""Capture ESPN credentials into .env, verify them, and find the team id.

Run it on this box:

    python3 scripts/espn-auth.py

It asks for the two cookie values, checks them against ESPN immediately, prints
the league's teams so you can pick Caroline's, and writes .env with mode 0600.
Nothing is echoed to the terminal and nothing is logged.

Standard library only, so it runs before `uv sync` has ever happened.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
DEFAULT_SEASON = 2026


def ask(prompt: str, *, secret: bool = False, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")
        raw = raw.strip()
        if raw:
            return raw
        if default is not None:
            return default
        print("  (required)")


def normalize_swid(value: str) -> str:
    """ESPN wants the braces. Losing them is the most common cause of a 401."""
    value = value.strip().strip('"').strip("'")
    if not value.startswith("{"):
        value = "{" + value
    if not value.endswith("}"):
        value = value + "}"
    return value


def fetch(url: str, cookies: dict[str, str]) -> tuple[int, dict | None]:
    jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, headers={"Cookie": jar, "User-Agent": "hal-mary/setup"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except urllib.error.URLError as exc:
        print(f"\n  Could not reach ESPN: {exc.reason}")
        sys.exit(2)


def describe(status: int) -> str:
    return {
        401: "Rejected. The cookies are wrong, expired, or belong to an account that\n"
        "  is not in this league. Re-copy both values and keep the braces on SWID.",
        404: "No such league for that season. Check LEAGUE_ID and SEASON.",
    }.get(status, f"ESPN returned HTTP {status}.")


def merge_env(updates: dict[str, str]) -> None:
    """Rewrite .env, replacing the keys we set and leaving everything else alone."""
    existing: list[str] = []
    if ENV_PATH.exists():
        existing = ENV_PATH.read_text().splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in existing:
        match = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        key = match.group(1) if match else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out).rstrip() + "\n")
    ENV_PATH.chmod(0o600)


def main() -> int:
    print(__doc__.split("Run it on this box:")[0].strip())
    print()
    print("In a browser on your own machine, signed in at fantasy.espn.com:")
    print("  F12 -> Application (Chrome) or Storage (Firefox) -> Cookies -> https://fantasy.espn.com")
    print("  Copy the values of `espn_s2` and `SWID`. Keep SWID's braces.")
    print("  Input is hidden; paste and press enter.")
    print()

    espn_s2 = ask("espn_s2", secret=True)
    swid = normalize_swid(ask("SWID", secret=True))
    league_id = ask("LEAGUE_ID (from the league page URL)")
    season = ask("SEASON", default=str(DEFAULT_SEASON))

    if not league_id.isdigit():
        print(f"\n  LEAGUE_ID must be a number, got {league_id!r}")
        return 2

    cookies = {"espn_s2": espn_s2, "SWID": swid}
    url = f"{BASE}/seasons/{season}/segments/0/leagues/{league_id}?" + urllib.parse.urlencode(
        [("view", "mTeam"), ("view", "mSettings")]
    )

    print("\nChecking with ESPN...")
    status, data = fetch(url, cookies)
    if status != 200 or data is None:
        print(f"  {describe(status)}")
        print("\nNothing was written. Run this again once you have fresh values.")
        return 1

    name = (data.get("settings") or {}).get("name", "(unnamed league)")
    teams = data.get("teams") or []
    print(f"  Works. League: {name} ({len(teams)} teams)\n")

    members = {m.get("id"): m for m in (data.get("members") or [])}
    print("  Teams in this league:")
    for team in teams:
        tid = team.get("id")
        label = team.get("name") or f"{team.get('location', '')} {team.get('nickname', '')}".strip()
        owner_ids = team.get("owners") or []
        owner = ""
        if owner_ids:
            member = members.get(owner_ids[0]) or {}
            first = member.get("firstName", "")
            last = member.get("lastName", "")
            owner = f" — {first} {last}".rstrip()
        print(f"    id {tid:>3}  {label}{owner}")

    print()
    team_id = ask("TEAM_ID (the id of Caroline's team, from the list above)")
    if not team_id.isdigit():
        print(f"\n  TEAM_ID must be a number, got {team_id!r}")
        return 2

    web_password = ""
    if not (ENV_PATH.exists() and re.search(r"^\s*WEB_PASSWORD\s*=\s*\S", ENV_PATH.read_text(), re.MULTILINE)):
        print("\n  Set a password for the web app. Anyone on your network who knows it")
        print("  can read the league. It is not the ESPN password.")
        web_password = ask("WEB_PASSWORD", secret=True)

    updates = {
        "ESPN_S2": espn_s2,
        "SWID": swid,
        "LEAGUE_ID": league_id,
        "TEAM_ID": team_id,
        "SEASON": season,
    }
    if web_password:
        updates["WEB_PASSWORD"] = web_password

    merge_env(updates)
    print(f"\nWrote {ENV_PATH} (mode 0600). It is gitignored.")
    print("These cookies expire after a few weeks. Re-run this when syncing starts failing.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was written.")
        raise SystemExit(130) from None
