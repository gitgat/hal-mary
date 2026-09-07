"""Shared plumbing for the Task 6b tests: a fake runner, a fake ESPN client, and
the real league's shape as committed fixtures.

Not a test module. It exists because the board-build job, the advisor and the
draft loop all need the same three things — a settings object built from a
temporary ``config.toml``, a database with the league in it, and a ``claude``
that answers without being spawned — and copying those into three files is how
they drift apart.

**Nothing here spawns a process or opens a socket.** ``FakeRunner`` stands in for
:class:`hal_mary.claude_runner.ClaudeRunner` at its own seam (``run``), which is
the seam every job calls through. The runner's *own* tests drive the real binary
through ``tests/fake_claude/claude``; these tests are about what the jobs do with
a result, so they hand one over directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FIXTURE_ENV
from hal_mary import db
from hal_mary.claude_runner import ClaudeResult
from hal_mary.config import Settings, load_settings

REPO = Path(__file__).resolve().parents[2]

# --- the real league, as confirmed from the live ESPN payload on 2026-09-07 ---

#: Six teams, snake, 90 seconds a pick, Caroline last in round 1.
REAL_TEAM_COUNT = 6
REAL_DRAFT_ORDER = [1, 2, 3, 4, 5, 6]
REAL_MY_TEAM_ID = 6

#: QB 1, RB 2, WR 2, TE 1, D/ST 1, K 1, RB/WR/TE 1, BE 7, IR 1. Sixteen of those
#: are drafted (everything but the IR slot), which is where "16 rounds" comes
#: from.
#:
#: The flex slot is spelled **RB/WR/TE**, not FLEX — that is what a live sync of
#: this league actually writes, and code or prose that only knows the word
#: "FLEX" silently stops describing the real roster.
REAL_ROSTER_SLOTS = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "D/ST": 1,
    "K": 1,
    "RB/WR/TE": 1,
    "BE": 7,
    "IR": 1,
}

#: ``settings`` as ESPN returns it, trimmed to the keys hal-mary reads.
REAL_RAW_SETTINGS = {
    "name": "Fantasy Football 2026",
    "size": REAL_TEAM_COUNT,
    "draftSettings": {
        "type": "SNAKE",
        "date": None,
        "timePerSelection": 90,
        "pickOrder": REAL_DRAFT_ORDER,
    },
    "scoringSettings": {
        "scoringType": "H2H_POINTS",
        "playerRankType": "PPR",
        # stat 53 is "each reception"; 1.0 is what makes this a full-PPR league.
        "scoringItems": [{"statId": 53, "points": 1.0}],
    },
}

#: A ``[league]`` block for ``config.toml`` describing the same league, for the
#: no-ESPN path. Deliberately *not* identical to the synced row, so a test can
#: tell which one won.
LEAGUE_TOML = """
[league]
team_count = 6
scoring_type = "full PPR"
points_per_reception = 1.0
draft_type = "SNAKE"
draft_order = [1, 2, 3, 4, 5, 6]
my_draft_slot = 6

[league.roster_slots]
QB = 1
RB = 2
WR = 2
TE = 1
"D/ST" = 1
K = 1
"RB/WR/TE" = 1
BE = 7
IR = 1
"""


def write_config(
    tmp_path: Path, extra: str = "", replace: dict[str, str] | None = None
) -> Path:
    """Copy the repo's ``config.toml`` into ``tmp_path``, edited for one test.

    ``extra`` is appended (a whole new section); ``replace`` swaps literal lines
    in place, which is how a test changes a value in a section that already
    exists — TOML rejects a table declared twice, so appending a second
    ``[draft]`` would not load at all.

    Copying rather than hand-writing means a test cannot pass against a config
    shape the shipped file does not have.
    """
    text = (REPO / "config.toml").read_text(encoding="utf-8")
    for old, new in (replace or {}).items():
        assert old in text, f"config.toml no longer contains {old!r}"
        text = text.replace(old, new, 1)
    path = tmp_path / "config.toml"
    path.write_text(text + "\n" + extra, encoding="utf-8")
    return path


def make_settings(
    tmp_path: Path, extra: str = "", replace: dict[str, str] | None = None, **env: str
) -> Settings:
    """Settings from a temporary config, with the fixture environment overlaid.

    ``claude.binary`` is left alone: these tests never spawn it. Paths that would
    otherwise be resolved against the repo (prompts, memory) stay pointed at the
    repo on purpose — ``prompts/board_build.md`` is a deliverable, and a test
    that read a stub instead would not notice it going missing.
    """
    values = {**FIXTURE_ENV, "TEAM_ID": str(REAL_MY_TEAM_ID), **env}
    return load_settings(config_path=write_config(tmp_path, extra, replace), env=values)


def open_db(tmp_path: Path):
    """A migrated database on local disk (never ``:memory:``: the loop reopens)."""
    conn = db.connect(tmp_path / "hal.db")
    db.migrate(conn)
    return conn


def seed_synced_league(conn, *, raw: dict[str, Any] | None = None, **overrides: Any) -> None:
    """Write the ``league_settings`` row a successful ``hal-mary sync`` leaves."""
    raw_settings = raw if raw is not None else REAL_RAW_SETTINGS
    row = {
        "season": 2026,
        "league_id": 1234567,
        "name": raw_settings.get("name"),
        "team_count": raw_settings.get("size"),
        "scoring_type": "H2H_POINTS",
        "draft_type": "SNAKE",
        "draft_date": None,
        "roster_slots_json": json.dumps(REAL_ROSTER_SLOTS),
        "raw_json": json.dumps(raw_settings, sort_keys=True),
    }
    row.update(overrides)
    conn.execute(
        """
        INSERT OR REPLACE INTO league_settings
            (id, season, league_id, name, team_count, scoring_type, draft_type,
             draft_date, roster_slots_json, raw_json, updated_at)
        VALUES (1, :season, :league_id, :name, :team_count, :scoring_type, :draft_type,
                :draft_date, :roster_slots_json, :raw_json, '2026-09-07T00:00:00+00:00')
        """,
        row,
    )
    conn.commit()


def seed_board(conn, rows: list[dict[str, Any]]) -> None:
    """Write board rows, filling in the columns a test does not care about."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO board
            (player_id, name, position, pro_team, tier, rank, bye_week, note,
             drafted_by_team_id, drafted_at, built_at)
        VALUES (:player_id, :name, :position, :pro_team, :tier, :rank, :bye_week, :note,
                :drafted_by_team_id, :drafted_at, '2026-09-07T00:00:00+00:00')
        """,
        [
            {
                "player_id": row.get("player_id"),
                "name": row["name"],
                "position": row.get("position"),
                "pro_team": row.get("pro_team"),
                "tier": row.get("tier"),
                "rank": row.get("rank"),
                "bye_week": row.get("bye_week"),
                "note": row.get("note"),
                "drafted_by_team_id": row.get("drafted_by_team_id"),
                "drafted_at": row.get("drafted_at"),
            }
            for row in rows
        ],
    )
    conn.commit()


#: A small, fully tiered board: enough shape for scarcity and needs to mean
#: something, small enough to reason about in an assertion.
SAMPLE_BOARD = [
    {"player_id": -1001, "name": "Ja'Marr Chase", "position": "WR", "pro_team": "CIN",
     "tier": 1, "rank": 1, "bye_week": 10, "note": "Catches a huge number of passes."},
    {"player_id": -1002, "name": "Bijan Robinson", "position": "RB", "pro_team": "ATL",
     "tier": 1, "rank": 2, "bye_week": 5, "note": "Runs and catches; on the field all game."},
    {"player_id": -1003, "name": "Justin Jefferson", "position": "WR", "pro_team": "MIN",
     "tier": 1, "rank": 3, "bye_week": 6, "note": "The best receiver in the league."},
    {"player_id": -1004, "name": "Saquon Barkley", "position": "RB", "pro_team": "PHI",
     "tier": 2, "rank": 4, "bye_week": 9, "note": "Scores a lot of touchdowns."},
    {"player_id": -1005, "name": "Brock Bowers", "position": "TE", "pro_team": "LV",
     "tier": 2, "rank": 5, "bye_week": 8, "note": "A tight end who is used like a receiver."},
    {"player_id": -1006, "name": "Josh Allen", "position": "QB", "pro_team": "BUF",
     "tier": 3, "rank": 6, "bye_week": 7, "note": "Throws and runs for points."},
    {"player_id": -1007, "name": "Puka Nacua", "position": "WR", "pro_team": "LAR",
     "tier": 3, "rank": 7, "bye_week": 6, "note": "Gets thrown to constantly when healthy."},
    {"player_id": -1008, "name": "Trey McBride", "position": "TE", "pro_team": "ARI",
     "tier": 4, "rank": 8, "bye_week": 8, "note": "Catches plenty; rarely scores."},
]


# --- fakes -------------------------------------------------------------------


def ok_result(structured: dict | None, text: str = "") -> ClaudeResult:
    """A successful call, with the structured payload the schema asked for."""
    return ClaudeResult(
        ok=True,
        text=text or json.dumps(structured or {}),
        structured=structured,
        session_id="fake-session",
        cost_usd=0.01,
        duration_ms=1200,
        exit_code=0,
        error=None,
        raw_path=None,
    )


def failed_result(text: str = "", error: str = "timed out") -> ClaudeResult:
    """A failed call that still carries the prose the model produced.

    ``ok=False`` with a non-empty ``text`` is the shape that matters: the advisor
    has to log what was actually said before falling back, or a bad
    recommendation cannot be reconstructed afterwards.
    """
    return ClaudeResult(
        ok=False,
        text=text,
        structured=None,
        session_id=None,
        cost_usd=None,
        duration_ms=None,
        exit_code=1,
        error=error,
        raw_path=None,
    )


class FakeRunner:
    """Replays queued results and records exactly how it was called.

    A queued item that is an ``Exception`` is raised instead of returned, which
    is how the "the runner itself blew up" path is driven.
    """

    def __init__(self, settings: Settings, results: list[Any] | None = None) -> None:
        self.settings = settings
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        job: str,
        prompt: str,
        *,
        schema: dict | None = None,
        resume: str | None = None,
        system_prompt: str | None = None,
        extra_context: str | None = None,
    ) -> ClaudeResult:
        self.calls.append(
            {
                "job": job,
                "prompt": prompt,
                "schema": schema,
                "resume": resume,
                "system_prompt": system_prompt,
                "extra_context": extra_context,
            }
        )
        if not self.results:
            raise AssertionError(f"fake runner ran out of results (call {len(self.calls)})")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeEspnClient:
    """Answers ``draft_picks`` and ``player_name_map`` from a script.

    ``picks`` is the list ESPN would return *now*; a test mutates it between
    polls. ``fail_next`` makes the next ``draft_picks`` raise, which is how the
    "an ESPN exception must not kill the loop" test is driven.
    """

    def __init__(self, picks: list[dict[str, Any]] | None = None) -> None:
        self.picks = list(picks or [])
        self.fail_next: Exception | None = None
        self.name_map_calls = 0
        self.draft_picks_calls = 0
        self.call_order: list[str] = []

    def player_name_map(self, refresh: bool = False) -> dict[int, str]:
        self.name_map_calls += 1
        self.call_order.append("player_name_map")
        return {}

    def draft_picks(self) -> list[dict[str, Any]]:
        self.draft_picks_calls += 1
        self.call_order.append("draft_picks")
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        return [dict(pick) for pick in self.picks]


class RecordingBus:
    """An :class:`~hal_mary.events.EventBus` stand-in that just remembers.

    The real bus needs a running event loop to subscribe to, and most of these
    tests are synchronous. One test uses the real bus to prove the wiring; the
    rest use this to assert on what was published.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, event: str, payload: dict) -> None:
        self.published.append((event, payload))
