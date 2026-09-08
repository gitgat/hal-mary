#!/usr/bin/env python
"""Fill a scratch database with a believable league that contains nobody real.

    uv run python scripts/demo_seed.py /tmp/demo/hal.db
    DB_PATH=/tmp/demo/hal.db uv run hal-mary serve

Two jobs, and the second is why this lives in `scripts/` rather than a test:

**Anyone can see the app without credentials.** hal-mary is useless to look at
empty — the draft page is a board, the team page is a roster — and the real one
needs ESPN cookies, a league id, and a Claude subscription. This gets a
stranger to a working UI in two commands.

**The screenshots in the README are taken against this.** The live league
contains four real people; a public README cannot. Every team here is `Team N`
owned by `Person N`, and the players are real NFL players, who are public
figures and the whole point of the picture.

The board is hand-written rather than model-generated on purpose: a demo that
needs a paid API call is a demo nobody runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hal_mary import db

TEAMS = [(i, f"Team {i}", f"TM{i:02d}", f"Person {i}") for i in range(1, 7)]
MY_TEAM = 6

#: Real NFL players — public figures, and the reason a screenshot reads as real.
#: Tiers and notes are written the way the board_build prompt asks for them:
#: plain English, for someone who knows the rules of football and nothing else.
BOARD = [
    (1, "Jahmyr Gibbs", "RB", "DET", 1, 5, "The consensus number one pick: he runs and he catches, and every catch is a point in this league."),
    (2, "Ja'Marr Chase", "WR", "CIN", 1, 10, "The best receiver in football, on a team that throws constantly."),
    (3, "Bijan Robinson", "RB", "ATL", 1, 5, "On the field for nearly every play, which is worth more here than raw talent."),
    (4, "Puka Nacua", "WR", "LAR", 1, 6, "Catches an enormous number of passes. Volume is what scores in this format."),
    (5, "Jaxon Smith-Njigba", "WR", "SEA", 1, 8, "His team's clear first read, and they throw more than most."),
    (6, "Amon-Ra St. Brown", "WR", "DET", 2, 5, "Take him with your first pick unless a top running back is still there."),
    (7, "Christian McCaffrey", "RB", "SF", 2, 9, "When healthy, the best pass-catching back in the game. The risk is the health."),
    (8, "Justin Jefferson", "WR", "MIN", 2, 6, "One of the two or three most talented receivers alive."),
    (9, "CeeDee Lamb", "WR", "DAL", 2, 10, "Elite receiver on a team that throws often and from behind."),
    (10, "Jonathan Taylor", "RB", "IND", 2, 14, "A huge share of the carries, and the touchdowns that come with them."),
    (11, "James Cook III", "RB", "BUF", 3, 7, "Scored a mountain of touchdowns and catches passes out of the backfield."),
    (12, "De'Von Achane", "RB", "MIA", 3, 12, "Small, fast, and used as a receiver as much as a runner."),
]

ROSTER = [
    ("Jahmyr Gibbs", "RB", "DET", "RB"),
    ("Ja'Marr Chase", "WR", "CIN", "WR"),
    ("Puka Nacua", "WR", "LAR", "WR"),
    ("Trey McBride", "TE", "ARI", "TE"),
    ("Jalen Hurts", "QB", "PHI", "QB"),
    ("Chase Brown", "RB", "CIN", "RB"),
    ("Nico Collins", "WR", "HOU", "RB/WR/TE"),
]


def seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    db.migrate(conn)
    raw = {
        "name": "The Demo League",
        "size": 6,
        "draftSettings": {"type": "SNAKE", "timePerSelection": 90, "pickOrder": [1, 2, 3, 4, 5, 6]},
        "scoringSettings": {"scoringType": "H2H_POINTS", "playerRankType": "PPR"},
        "scheduleSettings": {"matchupPeriodCount": 14, "playoffTeamCount": 4},
    }
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "D/ST": 1, "K": 1, "BE": 7, "RB/WR/TE": 1}
    with db.transaction(conn):
        conn.execute("DELETE FROM league_settings")
        conn.execute(
            """INSERT INTO league_settings
               (id, season, league_id, name, team_count, scoring_type, draft_type,
                roster_slots_json, raw_json, updated_at, current_week)
               VALUES (1, 2026, 1234567, ?, 6, 'H2H_POINTS', 'SNAKE', ?, ?, ?, 1)""",
            ("The Demo League", json.dumps(slots), json.dumps(raw), db.utc_now()),
        )
        conn.execute("DELETE FROM teams")
        for team_id, name, abbrev, owner in TEAMS:
            conn.execute(
                """INSERT INTO teams (team_id, name, abbrev, owner, draft_slot, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (team_id, name, abbrev, owner, team_id, db.utc_now()),
            )
        conn.execute("DELETE FROM board")
        for rank, name, pos, pro, tier, bye, note in BOARD:
            conn.execute(
                """INSERT INTO board (player_id, name, position, pro_team, tier, rank, bye_week,
                                      note, built_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (-1000 - rank, name, pos, pro, tier, rank, bye, note, db.utc_now()),
            )
        # A roster is two tables: `players` is who they are, `roster_slots` is
        # where they sit on a team in a given week.
        conn.execute("DELETE FROM roster_slots")
        for i, (name, pos, pro, slot) in enumerate(ROSTER, start=1):
            pid = 5000 + i
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (player_id, name, position, pro_team, updated_at) VALUES (?,?,?,?,?)""",
                (pid, name, pos, pro, db.utc_now()),
            )
            conn.execute(
                """INSERT INTO roster_slots (team_id, player_id, slot, week, updated_at)
                   VALUES (?,?,?,?,?)""",
                (MY_TEAM, pid, slot, 1, db.utc_now()),
            )
    conn.close()

    # The env file, so the two commands in the README actually work. These are
    # not credentials: ESPN is never called in demo mode -- the database is
    # already populated and no job is run.
    env = path.parent / "env"
    env.write_text(
        "\n".join(
            [
                "ESPN_S2=demo-not-a-real-cookie",
                "SWID={00000000-0000-0000-0000-000000000000}",
                "LEAGUE_ID=1234567",
                "TEAM_ID=6",
                "SEASON=2026",
                "WEB_PASSWORD=demo",
                f"DB_PATH={path}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env.chmod(0o600)

    print(f"seeded {path}")
    print("  6 teams (Team 1..Team 6, owned by Person 1..Person 6) — nobody real")
    print(f"  {len(BOARD)} players on the board, {len(ROSTER)} on the roster")
    print(f"  env written to {env}  (password: demo)")
    print(f"\nHAL_MARY_ENV={env} uv run hal-mary serve")


if __name__ == "__main__":
    seed(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hal-mary-demo/hal.db"))
