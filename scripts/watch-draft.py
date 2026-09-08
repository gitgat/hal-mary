#!/usr/bin/env python3
"""Watch any ESPN draft board and print what changes, once a second.

The one thing nobody has been able to answer without a live draft: **does ESPN
populate ``draftDetail.picks`` while a draft is in progress, or only once it is
over?** Everything on draft night depends on the answer, and the manual
pick-entry path exists because we do not have it.

Point this at a mock draft — or at the real league during the real draft — and
it answers the question in about thirty seconds.

    python3 scripts/watch-draft.py                # the league in .env
    python3 scripts/watch-draft.py 1234567        # any other league id

Reads ESPN_S2 / SWID / SEASON from .env. Read-only: it fetches one URL and
prints. It never writes to ESPN and never touches hal-mary's database.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if not path.exists():
        sys.exit(f"no {path} — run scripts/espn-auth.py first")
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def fetch(url: str, cookies: dict[str, str]) -> tuple[int, dict | None]:
    jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, headers={"Cookie": jar, "User-Agent": "hal-mary/watch"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except urllib.error.URLError as exc:
        print(f"  network: {exc.reason}")
        return 0, None


def made(pick: dict) -> bool:
    """A pick counts only when a real player is attached to it.

    ESPN pre-populates every slot before the draft starts, so a row's existence
    means nothing. This is the same rule ``espn.client.pick_is_made`` applies.
    """
    pid = pick.get("playerId")
    return isinstance(pid, int) and pid > 0


def main() -> int:
    env = load_env()
    league = sys.argv[1] if len(sys.argv) > 1 else env.get("LEAGUE_ID")
    season = env.get("SEASON", "2026")
    if not league:
        sys.exit("no league id: pass one as an argument or set LEAGUE_ID in .env")

    cookies = {"espn_s2": env.get("ESPN_S2", ""), "SWID": env.get("SWID", "")}
    url = f"{BASE}/seasons/{season}/segments/0/leagues/{league}?" + urllib.parse.urlencode(
        {"view": "mDraftDetail"}
    )

    print(f"watching league {league}, season {season} — one request a second, Ctrl-C to stop")
    print("looking for: does the number of MADE picks go up while the draft runs?\n")
    print(f"{'time':<10} {'http':<5} {'inProg':<7} {'drafted':<8} {'slots':<6} {'MADE':<5} latest")
    print("-" * 78)

    last_made = -1
    while True:
        status, payload = fetch(url, cookies)
        stamp = time.strftime("%H:%M:%S")

        if status == 401:
            print(f"{stamp:<10} 401 — cookies rejected. Re-run scripts/espn-auth.py.")
            return 1
        if status != 200 or payload is None:
            print(f"{stamp:<10} {status:<5} (no payload)")
            time.sleep(1)
            continue

        detail = payload.get("draftDetail") or {}
        picks = detail.get("picks") or []
        real = [p for p in picks if made(p)]
        latest = ""
        if real:
            last = max(real, key=lambda p: p.get("overallPickNumber") or 0)
            latest = (
                f"#{last.get('overallPickNumber')} team {last.get('teamId')} "
                f"player {last.get('playerId')}"
            )

        line = (
            f"{stamp:<10} {status:<5} {str(detail.get('inProgress')):<7} "
            f"{str(detail.get('drafted')):<8} {len(picks):<6} {len(real):<5} {latest}"
        )
        if len(real) != last_made:
            line += "   <-- CHANGED"
            last_made = len(real)
        print(line)
        time.sleep(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        raise SystemExit(0) from None
