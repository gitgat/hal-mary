"""Tests for the web app: auth, the pages, and the event stream.

Every test builds the app over a temporary database through ``create_app``'s
injection seams, so nothing here touches the real ``hal.db``, ESPN, or the
``claude`` binary. ``tests/conftest.py`` blocks both HTTP stacks; these tests
never need them because the two things that would reach out — the ESPN auth
check and the sync — are injected.

The state most worth testing is the *empty* database. That is the box on the
morning before the draft, and a page that only ever rendered against a populated
fixture is the page that breaks in front of Caroline.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape  # what Jinja actually uses: &#39; not &#x27;

from conftest import FIXTURE_ENV
from hal_mary import db
from hal_mary.config import Settings, load_settings
from hal_mary.events import EventBus

PASSWORD = FIXTURE_ENV["WEB_PASSWORD"]

#: Caroline's team in these tests. Her real id comes from settings.team_id and
#: is never hardcoded in src/.
HER_TEAM_ID = 6

# Every identity below is invented. `hal-mary sync` writes the real league id
# and the real leaguemates' names into memory/league.md, and CLAUDE.md forbids
# those reaching a test fixture: a name in git history cannot be removed by a
# later commit. Invented data exercises exactly the same code paths — the
# apostrophe in team five is here to keep the escaping covered, and the two
# nameless teams mirror the "owner unknown" case ESPN really does produce.
FAKE_LEAGUE_ID = 7654321
FAKE_LEAGUE_NAME = "The Invented League"
HER_TEAM_NAME = "Hail Mary Hopefuls"
HER_OWNER = "Nora Testcase"
FAKE_TEAMS = (
    (1, "Gridiron Gerbils", "Dana Fixture", "GERB", 1),
    (2, "Team 2", None, "TM2", 2),
    (3, "Team 3", None, "TM3", 3),
    (4, "Punt Intended", "Robin Placeholder", "PUNT", 4),
    (5, "Marla's Marvellous Squad", "Marla Example", "MMS", 5),
    (HER_TEAM_ID, HER_TEAM_NAME, HER_OWNER, "HMH", 6),
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A migrated, empty database on disk."""
    path = tmp_path / "hal.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


def make_settings(db_path: Path, **overrides: str):
    """Settings from the real config.toml with a temporary database."""
    env = {**FIXTURE_ENV, "TEAM_ID": str(HER_TEAM_ID), "DB_PATH": str(db_path)}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return load_settings(env=env)


def open_conn(db_path: Path) -> sqlite3.Connection:
    return db.connect(db_path)


def build_app(db_path: Path, **kwargs):
    from hal_mary.web.app import create_app

    settings = kwargs.pop("settings", None) or make_settings(db_path)
    kwargs.setdefault("check_auth", lambda: (True, "ESPN credentials are valid"))
    return create_app(settings, connect=lambda: open_conn(db_path), **kwargs)


def client_for(db_path: Path, **kwargs) -> TestClient:
    return TestClient(build_app(db_path, **kwargs), follow_redirects=False)


def login(client: TestClient, password: str = PASSWORD):
    return client.post("/login", data={"password": password})


def post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    """A state-changing post carrying the CSRF token, as a rendered form does.

    Every POST on the private router is checked, so a test that posts without
    one is testing the refusal rather than the handler.
    """
    settings = load_settings(env={**FIXTURE_ENV, "TEAM_ID": str(HER_TEAM_ID)})
    token = client.cookies[settings.web.csrf_cookie]
    return client.post(url, data={"csrf_token": token, **(data or {})}, **kwargs)


def flatten_routes(app: Any) -> list[Any]:
    """Every real route, through whatever wrapper the framework put in the way.

    `include_router` does not flatten its routes into `app.routes` in this
    FastAPI: it appends one `_IncludedRouter` object that keeps the original
    router inside it. Anything walking `app.routes` naively sees three
    pathless objects and concludes the app has no routes to check.
    """
    found: list[Any] = []
    for route in getattr(app, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(flatten_routes(inner))
        else:
            found.append(route)
    return found


def populate_league(db_path: Path) -> None:
    """Six teams, a league row, and Caroline's roster part-drafted."""
    conn = open_conn(db_path)
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO league_settings
                (id, season, league_id, name, team_count, scoring_type, draft_type,
                 draft_date, roster_slots_json, raw_json, updated_at)
            VALUES (1, 2026, ?, ?, 6, 'H2H_POINTS', 'SNAKE',
                    NULL, ?, '{}', '2026-09-07T20:13:54+00:00')
            """,
            (
                FAKE_LEAGUE_ID,
                FAKE_LEAGUE_NAME,
                json.dumps(
                    {
                        "QB": 1,
                        "RB": 2,
                        "WR": 2,
                        "TE": 1,
                        "D/ST": 1,
                        "K": 1,
                        "BE": 7,
                        "IR": 1,
                        "RB/WR/TE": 1,
                    }
                ),
            ),
        )
        conn.executemany(
            "INSERT INTO teams (team_id, name, owner, abbrev, draft_slot, updated_at)"
            " VALUES (?, ?, ?, ?, ?, '2026-09-07T20:13:54+00:00')",
            FAKE_TEAMS,
        )
    conn.close()


def populate_roster(db_path: Path) -> None:
    """Two players on Caroline's team, one of them hurt."""
    conn = open_conn(db_path)
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO players (player_id, name, position, pro_team, injury_status,"
            " updated_at) VALUES (?, ?, ?, ?, ?, '2026-09-07T20:13:54+00:00')",
            [
                (4430807, "Bijan Robinson", "RB", "ATL", "ACTIVE"),
                (4362628, "Ja'Marr Chase", "WR", "CIN", "QUESTIONABLE"),
            ],
        )
        conn.executemany(
            "INSERT INTO roster_slots (team_id, player_id, slot, week, updated_at)"
            " VALUES (?, ?, ?, NULL, '2026-09-07T20:13:54+00:00')",
            [(HER_TEAM_ID, 4430807, "RB"), (HER_TEAM_ID, 4362628, "WR")],
        )
        conn.execute(
            "INSERT INTO board (player_id, name, position, pro_team, tier, rank, bye_week)"
            " VALUES (4430807, 'Bijan Robinson', 'RB', 'ATL', 1, 2, 12)"
        )
    conn.close()


# --- refusing to start ------------------------------------------------------


def test_create_app_refuses_to_start_without_a_password(db_path: Path):
    """It listens on every interface and the database holds ESPN cookies."""
    from hal_mary.web.app import MissingPasswordError, create_app

    settings = make_settings(db_path, WEB_PASSWORD=None)
    with pytest.raises(MissingPasswordError) as excinfo:
        create_app(settings, connect=lambda: open_conn(db_path))
    assert "WEB_PASSWORD" in str(excinfo.value)


def test_create_app_refuses_an_empty_password(db_path: Path):
    from hal_mary.web.app import MissingPasswordError, create_app

    settings = make_settings(db_path).model_copy(update={"web_password": "   "})
    with pytest.raises(MissingPasswordError):
        create_app(settings, connect=lambda: open_conn(db_path))


# --- auth -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/status", "/team", "/league", "/draft", "/draft/live", "/events"]
)
def test_unauthenticated_pages_redirect_to_login(db_path: Path, path: str):
    with client_for(db_path) as client:
        response = client.get(path)
    assert response.status_code in (302, 303, 307)
    assert response.headers["location"].startswith("/login")


def test_no_route_escapes_the_password_by_accident(db_path: Path):
    """Walk the whole app, not just the routes someone remembered to test.

    A page added to the wrong router would otherwise serve Caroline's league —
    and eventually her advice and her chat — to anyone on the network, and
    nothing would fail.
    """
    public = {"/login", "/healthz", "/static"}
    app = build_app(db_path)
    routes = flatten_routes(app)

    # Without this the walk is vacuous — which is exactly what it was. This
    # FastAPI represents an included router as one opaque route object, so
    # `app.routes` held three entries with no `.path` between them and the loop
    # below checked nothing at all while appearing to check everything.
    found = {route.path for route in routes if getattr(route, "path", None)}
    assert {
        "/",
        "/status",
        "/team",
        "/league",
        "/draft",
        "/draft/live",
        "/draft/pick",
        "/draft/unmatched/resolve",
        "/sync",
        "/events",
        "/logout",
    } <= found, (
        f"the route walk found only {sorted(found)}"
    )

    with TestClient(app, follow_redirects=False) as client:
        for route in routes:
            path = getattr(route, "path", None)
            if path is None or path in public or path.startswith("/static"):
                continue
            for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
                response = client.request(method, path)
                assert response.status_code in (302, 303, 307), f"{method} {path} is unprotected"
                # The guard's own redirect, not merely one that lands on /login.
                # A public route that redirects there under its own steam —
                # /logout used to — would otherwise walk straight past this.
                assert response.headers["location"] == f"/login?next={path}", (
                    f"{method} {path} redirects to /login without being guarded"
                )


def test_unauthenticated_sync_is_refused(db_path: Path):
    calls = []
    with client_for(db_path, run_sync=lambda: calls.append(1)) as client:
        response = client.post("/sync")
    assert response.status_code in (302, 303, 307)
    assert calls == []


def test_healthz_needs_no_password(db_path: Path):
    with client_for(db_path) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_static_files_need_no_password(db_path: Path):
    with client_for(db_path) as client:
        response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_login_page_renders(db_path: Path):
    with client_for(db_path) as client:
        response = client.get("/login")
    assert response.status_code == 200
    assert "password" in response.text.lower()


def test_correct_password_grants_access(db_path: Path):
    with client_for(db_path) as client:
        response = login(client)
        assert response.status_code in (302, 303)
        assert client.get("/status").status_code == 200


def test_wrong_password_is_refused_and_reveals_nothing(db_path: Path):
    with client_for(db_path) as client:
        response = login(client, "not-the-password")
        assert response.status_code == 401
        assert client.cookies.get(load_settings(env=FIXTURE_ENV).web.session_cookie) is None
        body = response.text.lower()
        # No hint about which part was wrong, and never an echo of the real one.
        assert PASSWORD not in response.text
        for leak in ("too short", "too long", "close", "almost", "character"):
            assert leak not in body
        assert client.get("/status").status_code in (302, 303, 307)


def test_session_cookie_is_httponly_and_samesite(db_path: Path):
    with client_for(db_path) as client:
        response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    # Not served over TLS on the LAN: a Secure cookie would never be sent back.
    assert "secure" not in cookie


def test_logout_clears_the_session(db_path: Path):
    with client_for(db_path) as client:
        login(client)
        assert client.get("/status").status_code == 200
        post(client, "/logout")
        assert client.get("/status").status_code in (302, 303, 307)


def test_logout_requires_a_session(db_path: Path):
    """Otherwise any page on the network can log her out mid-draft."""
    with client_for(db_path) as client:
        response = client.post("/logout")
    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login?next=/logout"


# --- the key the cookie is signed with, and guessing the password ------------


def test_the_signing_key_is_a_kdf_not_a_bare_hash():
    """The cookie crosses the LAN in cleartext.

    Anyone who captures one — a guest device, a phone backup, a router log —
    can brute-force the household password offline against it. A single SHA-256
    is a few billion guesses a second; a memory-hard KDF is not.
    """
    from hal_mary.web.app import SESSION_SALT, session_key

    key = session_key("hunter2")
    assert key == session_key("hunter2"), "the key must be stable across restarts"
    assert key != session_key("hunter3")
    assert key != hashlib.sha256(f"{SESSION_SALT}:hunter2".encode()).hexdigest()
    assert key == hashlib.scrypt(
        b"hunter2", salt=SESSION_SALT.encode(), n=2**14, r=8, p=1, dklen=32
    ).hex()


def test_login_locks_out_after_repeated_wrong_passwords(db_path: Path, caplog):
    settings = make_settings(db_path)
    limit = settings.web.login_max_attempts
    with caplog.at_level(logging.WARNING), client_for(db_path, settings=settings) as client:
        for _ in range(limit):
            assert login(client, "wrong").status_code == 401
        locked = login(client, "wrong")
        assert locked.status_code == 429
        # Even the right password is refused while locked out, or the limit is
        # only slowing down someone who was never going to guess it.
        assert login(client).status_code == 429
        assert client.get("/status").status_code in (302, 303, 307)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "login" in logged.lower()
    assert PASSWORD not in logged, "never log the password that was tried"


def test_the_lockout_is_per_client_not_a_way_to_lock_her_out(db_path: Path):
    """One noisy device on the network must not deny Caroline her own app."""
    app = build_app(db_path)
    settings = make_settings(db_path)
    with TestClient(app, follow_redirects=False, client=("192.168.1.99", 4321)) as attacker:
        for _ in range(settings.web.login_max_attempts + 1):
            attacker.post("/login", data={"password": "wrong"})
        assert attacker.post("/login", data={"password": PASSWORD}).status_code == 429

    with TestClient(app, follow_redirects=False, client=("192.168.1.50", 5555)) as hers:
        assert hers.post("/login", data={"password": PASSWORD}).status_code == 303


def test_a_successful_login_forgives_the_earlier_fumbles(db_path: Path):
    settings = make_settings(db_path)
    with client_for(db_path, settings=settings) as client:
        for _ in range(settings.web.login_max_attempts - 1):
            login(client, "wrong")
        assert login(client).status_code == 303
        # The counter is clear, so a later typo does not land her at the limit.
        assert login(client, "wrong").status_code == 401


def test_login_limiter_forgets_after_the_lockout_expires():
    from hal_mary.web.app import LoginLimiter

    clock = [1000.0]
    limiter = LoginLimiter(max_attempts=3, lockout_seconds=60, clock=lambda: clock[0])
    for _ in range(3):
        assert limiter.locked_out("phone") is False
        limiter.record_failure("phone")
    assert limiter.locked_out("phone") is True

    clock[0] += 59
    assert limiter.locked_out("phone") is True
    clock[0] += 2
    assert limiter.locked_out("phone") is False


def test_a_forged_cookie_does_not_authenticate(db_path: Path):
    settings = make_settings(db_path)
    with client_for(db_path) as client:
        client.cookies.set(settings.web.session_cookie, "hal-mary-authenticated")
        assert client.get("/status").status_code in (302, 303, 307)


def test_a_cookie_signed_with_another_password_is_rejected(db_path: Path):
    """Changing WEB_PASSWORD invalidates every outstanding session."""
    with client_for(db_path) as client:
        login(client)
        cookie = client.cookies[make_settings(db_path).web.session_cookie]

    other = make_settings(db_path, WEB_PASSWORD="a-different-password")
    with client_for(db_path, settings=other) as client:
        client.cookies.set(other.web.session_cookie, cookie)
        assert client.get("/status").status_code in (302, 303, 307)


def test_login_sends_her_on_to_where_she_was_going(db_path: Path):
    with client_for(db_path) as client:
        redirect = client.get("/team")
        assert "next=" in redirect.headers["location"]
        response = client.post("/login", data={"password": PASSWORD, "next": "/team"})
        assert response.headers["location"] == "/team"


def test_login_ignores_an_offsite_next(db_path: Path):
    with client_for(db_path) as client:
        response = client.post(
            "/login", data={"password": PASSWORD, "next": "https://evil.example/x"}
        )
    assert response.headers["location"] == "/status"


# --- pages on an empty database --------------------------------------------


@pytest.mark.parametrize("path", ["/status", "/team", "/league", "/draft"])
def test_every_page_renders_on_an_empty_database(db_path: Path, path: str):
    with client_for(db_path) as client:
        login(client)
        response = client.get(path)
    assert response.status_code == 200
    assert "<html" in response.text.lower()


def test_root_redirects_to_the_draft_page(db_path: Path):
    """The draft page is what she opens on the night, so it is where / lands."""
    with client_for(db_path) as client:
        login(client)
        response = client.get("/")
    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/draft"


# --- /team ------------------------------------------------------------------


def test_team_page_on_an_empty_roster_says_nothing_drafted(db_path: Path):
    populate_league(db_path)
    with client_for(db_path) as client:
        login(client)
        response = client.get("/team")
    assert response.status_code == 200
    assert "nothing drafted yet" in response.text.lower()


def test_team_page_shows_open_slots_as_empty_rows(db_path: Path):
    populate_league(db_path)
    with client_for(db_path) as client:
        login(client)
        text = client.get("/team").text
    # Every configured starting slot is visible even with nobody in it — and
    # named, because "QB" is not a word Caroline has any reason to know.
    for slot in ("Quarterback", "Running back", "Wide receiver", "Tight end"):
        assert slot in text
    assert "empty" in text.lower()


def test_team_page_names_slots_and_positions_in_words(db_path: Path):
    """``web/positions.py`` states the rule; this page has to follow it too.

    It used to print the slot code in brackets after the heading it had just
    spelled out, and the raw position beside every player.
    """
    populate_league(db_path)
    populate_roster(db_path)
    with client_for(db_path) as client:
        login(client)
        text = client.get("/team").text
    assert "running back · ATL" in text, "a player's position belongs in words"
    for code in ("(QB)", "(RB)", "(WR)", "(TE)", "(RB/WR/TE)", "(BE)"):
        assert code not in text, f"{code} is a code standing on its own"


def test_team_page_shows_drafted_players_with_injury_and_bye(db_path: Path):
    populate_league(db_path)
    populate_roster(db_path)
    with client_for(db_path) as client:
        login(client)
        text = client.get("/team").text
    assert "Bijan Robinson" in text
    assert escape("Ja'Marr Chase") in text
    assert "QUESTIONABLE" in text
    # The bye week comes from the board row, and only the player who has one
    # shows it: a stray "12" anywhere on the page would pass a laxer assertion.
    assert "Bye week 12" in text
    assert text.count("Bye week") == 1
    assert "nothing drafted yet" not in text.lower()


# --- /league ----------------------------------------------------------------


def league_rows(text: str) -> list[str]:
    """The rendered team rows, so an assertion can be about one row.

    "the marker is somewhere on the page" is not the claim worth testing — a
    page that told Caroline the wrong team was hers would pass that.
    """
    rows = text.split('<div class="team')[1:]
    assert len(rows) == len(FAKE_TEAMS), "expected one row per team"
    return rows


def test_league_page_lists_six_teams_and_marks_hers(db_path: Path):
    populate_league(db_path)
    with client_for(db_path) as client:
        login(client)
        text = client.get("/league").text

    for _id, name, owner, _abbrev, _slot in FAKE_TEAMS:
        assert escape(name) in text
        if owner:
            assert escape(owner) in text
    assert "owner unknown" in text  # the two teams ESPN gives no owner for

    marked = [row for row in league_rows(text) if "Your team" in row]
    assert len(marked) == 1, "exactly one team is hers"
    assert escape(HER_TEAM_NAME) in marked[0]
    assert escape(HER_OWNER) in marked[0]


def test_league_page_shows_the_draft_order(db_path: Path):
    populate_league(db_path)
    with client_for(db_path) as client:
        login(client)
        text = client.get("/league").text
    assert "draft order" in text.lower()

    rows = league_rows(text)
    picks = [re.search(r'<span class="pick">([^<]*)</span>', row).group(1) for row in rows]
    assert picks == ["1", "2", "3", "4", "5", "6"], "teams are listed in draft order"
    # Hers is the sixth pick, and the badge that says so is in her row.
    assert '<span class="pick">6</span>' in next(row for row in rows if "Your team" in row)


# --- /status ----------------------------------------------------------------


def test_status_reports_missing_secrets(db_path: Path):
    settings = make_settings(db_path, ESPN_S2=None)
    with client_for(db_path, settings=settings) as client:
        login(client)
        text = client.get("/status").text
    assert "ESPN_S2" in text
    assert "missing" in text.lower()


def test_status_reports_a_failing_espn_auth_loudly(db_path: Path):
    def failing():
        return False, "ESPN credentials have expired: 401"

    with client_for(db_path, check_auth=failing) as client:
        login(client)
        text = client.get("/status").text
    assert "ESPN credentials have expired" in text
    assert "problem" in text.lower() or "wrong" in text.lower()


def test_status_reports_a_stale_sync_with_its_age(db_path: Path):
    from datetime import UTC, datetime, timedelta

    then = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
    conn = open_conn(db_path)
    conn.execute(
        "INSERT INTO sync_runs (kind, started_at, finished_at, status)"
        " VALUES ('league', ?, ?, 'ok')",
        (then, then),
    )
    conn.close()
    with client_for(db_path) as client:
        login(client)
        text = client.get("/status").text
    assert "3 hours ago" in text


def test_status_reports_a_sync_that_never_ran(db_path: Path):
    """Both syncs, by name, in the red list and in their own rows."""
    with client_for(db_path) as client:
        login(client)
        text = client.get("/status").text
    assert "League and rosters have never been synced from ESPN." in text
    assert "Draft picks have never been synced from ESPN." in text
    assert text.count("never · never run") == 2


def test_status_shows_row_counts_and_recent_job_runs(db_path: Path):
    populate_league(db_path)
    conn = open_conn(db_path)
    conn.execute(
        "INSERT INTO job_runs (job, started_at, finished_at, status, summary)"
        " VALUES ('board_build', '2026-09-07T06:00:00+00:00',"
        " '2026-09-07T06:03:00+00:00', 'ok', 'built 180 players')"
    )
    conn.close()
    with client_for(db_path) as client:
        login(client)
        text = client.get("/status").text
    for label in ("teams", "players", "board", "notes", "advice"):
        assert label in text.lower()
    assert "board_build" in text
    assert "built 180 players" in text


def test_status_reports_the_claude_binary(db_path: Path):
    with client_for(db_path) as client:
        login(client)
        text = client.get("/status").text
    assert "claude" in text.lower()


def test_status_does_not_call_espn_twice_in_a_row(db_path: Path):
    """The auth check is a network call; the status page is refreshed often."""
    calls = []

    def counting():
        calls.append(1)
        return True, "ok"

    with client_for(db_path, check_auth=counting) as client:
        login(client)
        client.get("/status")
        client.get("/status")
    assert len(calls) == 1


def test_status_survives_an_exploding_auth_check(db_path: Path):
    def boom():
        raise RuntimeError("ESPN fell over")

    with client_for(db_path, check_auth=boom) as client:
        login(client)
        response = client.get("/status")
    assert response.status_code == 200
    assert "ESPN fell over" in response.text


# --- /sync ------------------------------------------------------------------


def test_sync_runs_off_the_event_loop_and_reports_the_outcome(db_path: Path):
    import asyncio
    import threading

    seen: dict[str, object] = {}

    def fake_sync():
        seen["thread"] = threading.current_thread().name
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen["on_loop"] = False
        else:
            seen["on_loop"] = True
        return {"teams": 6, "players": 200, "picks": 3}

    with client_for(db_path, run_sync=fake_sync) as client:
        login(client)
        response = post(client, "/sync", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert seen["on_loop"] is False
    assert "6" in response.text and "200" in response.text


def test_sync_failure_is_reported_not_raised(db_path: Path):
    def failing_sync():
        raise RuntimeError("ESPN said 401")

    with client_for(db_path, run_sync=failing_sync) as client:
        login(client)
        response = post(client, "/sync", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "ESPN said 401" in response.text


def test_sync_without_htmx_redirects_back_to_status(db_path: Path):
    with client_for(db_path, run_sync=lambda: {"teams": 6}) as client:
        login(client)
        response = post(client, "/sync")
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/status"


def test_two_taps_do_not_start_two_syncs(db_path: Path):
    """``hx-disabled-elt`` only disables the button on the HTMX path.

    A second tap — a double tap on a phone, a second device, the non-HTMX
    fallback — would otherwise put two writers into SQLite and fire a second
    round of requests at an unofficial ESPN endpoint that rate-limits.
    """
    running = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_sync():
        calls.append(1)
        running.set()
        release.wait(timeout=5)
        return {"teams": 6}

    with client_for(db_path, run_sync=slow_sync) as client:
        login(client)
        first: dict[str, object] = {}

        def run_first() -> None:
            first["response"] = post(client, "/sync", headers={"HX-Request": "true"})

        thread = threading.Thread(target=run_first)
        thread.start()
        try:
            assert running.wait(timeout=5), "the first sync never started"
            second = post(client, "/sync", headers={"HX-Request": "true"})
        finally:
            release.set()
            thread.join(timeout=5)

    assert calls == [1], "the second tap must not start a second sync"
    assert second.status_code == 200
    assert "already running" in second.text.lower()
    # And it is told as news, not as a failure: nothing went wrong.
    assert "failed" not in second.text.lower()
    assert "teams 6" in str(first["response"].text)


# --- /events ----------------------------------------------------------------


@pytest.fixture
def session_cookie(db_path: Path) -> str:
    """A valid session cookie header.

    The signing key comes from the password, not from the app object, so a
    cookie minted here authenticates against any app built with the same
    password — which is what lets the streaming tests below build their own.
    """
    name = make_settings(db_path).web.session_cookie
    with client_for(db_path) as client:
        login(client)
        return f"{name}={client.cookies[name]}"


class EventProbe:
    """Drives ``/events`` at the ASGI layer, because a client cannot.

    Starlette's ``TestClient`` and httpx's ASGI transport both buffer the whole
    response before handing it back, so an endless stream deadlocks them — and
    neither can deliver an ``http.disconnect``, which is the event this endpoint
    has to survive. Calling the app directly is the only way to test the thing
    that actually happens when a phone locks its screen.

    ``spec_version`` is 2.3 to match uvicorn, so this exercises the same
    disconnect path production takes.
    """

    def __init__(self, app, cookie: str, path: str = "/events") -> None:
        self.app = app
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
            "client": ("192.168.1.50", 51234),
            "server": ("192.168.1.184", 8080),
            "state": {},
        }
        self.sent: asyncio.Queue = asyncio.Queue()
        self.disconnected = asyncio.Event()
        self.request_sent = False
        self.status: int | None = None
        self.headers: dict[str, str] = {}

    async def _receive(self) -> dict:
        if not self.request_sent:
            self.request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        await self.sent.put(message)

    async def __aenter__(self) -> Self:
        self.task = asyncio.ensure_future(self.app(self.scope, self._receive, self._send))
        start = await asyncio.wait_for(self.sent.get(), timeout=3)
        assert start["type"] == "http.response.start"
        self.status = start["status"]
        self.headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return self

    async def frame(self) -> str:
        """The next non-empty body chunk, as text."""
        while True:
            message = await asyncio.wait_for(self.sent.get(), timeout=3)
            body = message.get("body", b"")
            if body:
                return body.decode()

    async def __aexit__(self, *_exc: object) -> None:
        self.disconnected.set()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(self.task, timeout=3)


async def test_events_streams_a_published_event(db_path: Path, session_cookie: str):
    bus = EventBus()
    app = build_app(db_path, bus=bus)
    async with EventProbe(app, session_cookie) as probe:
        assert probe.status == 200
        assert "text/event-stream" in probe.headers["content-type"]
        assert (await probe.frame()).startswith(":")  # open, therefore subscribed
        bus.publish("board_updated", {"overall_pick": 7})
        frame = await probe.frame()
    assert "event: board_updated" in frame
    payload = json.loads(frame.split("data:", 1)[1].strip())
    assert payload == {"overall_pick": 7}


async def test_events_removes_the_subscriber_on_disconnect(db_path: Path, session_cookie: str):
    """A phone that sleeps must not leave its subscription behind."""
    bus = EventBus()
    app = build_app(db_path, bus=bus)
    async with EventProbe(app, session_cookie) as probe:
        await probe.frame()
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


async def test_event_stream_response_closes_its_iterator_when_cancelled():
    """The narrow window ``EventStreamResponse`` exists for.

    Usually a disconnect arrives while the generator is parked at its own
    ``await``; cancelling that frame unwinds ``async with bus.subscribe()`` on
    the way out, and the subscription goes whether or not anything closes the
    generator. The leak is when the disconnect lands while ``stream_response``
    is inside ``await send()``: the generator is then suspended at its
    ``yield``, *outside* the cancelled frame. Nothing unwinds it, and Starlette
    does not close it.

    Reproducing that needs one more thing than a cancelled request, which is
    why the end-to-end test above passes either way: CPython refcounting
    usually finalises the orphaned generator during the very next await, and
    the finaliser closes it for us. It only stays open while something still
    holds the response — a traceback, a background task, a reference like the
    one this test keeps. Then the subscription outlives the connection until
    the garbage collector happens to look.
    """
    from hal_mary.web.app import EventStreamResponse, event_stream

    bus = EventBus()
    writing = asyncio.Event()
    # Held for the whole test, so nothing is collected behind our back.
    response = EventStreamResponse(event_stream(bus, 30.0), media_type="text/event-stream")

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            writing.set()
            await asyncio.Event().wait()  # a client that has stopped reading

    task = asyncio.ensure_future(response.stream_response(send))
    await asyncio.wait_for(writing.wait(), timeout=3)
    assert bus.subscriber_count == 1

    task.cancel()  # what Starlette does when the client disconnects
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert bus.subscriber_count == 0, (
        "the subscription outlived the connection: /events must close its "
        "generator explicitly rather than leave it to the garbage collector"
    )


def test_events_is_served_by_the_response_that_closes_its_generator(db_path: Path):
    """Belt and braces for the test above, which is subtle enough to be
    deleted by someone who does not see what it is for."""
    from hal_mary.web.app import EventStreamResponse

    app = build_app(db_path)
    route = next(r for r in flatten_routes(app) if getattr(r, "path", None) == "/events")
    assert route.response_class is EventStreamResponse


async def test_event_stream_generator_unsubscribes_when_closed():
    """The generator itself cleans up, however it is torn down."""
    from hal_mary.web.app import event_stream

    bus = EventBus()
    stream = event_stream(bus, heartbeat_s=0.01)
    first = await anext(stream)
    assert first.startswith(":")
    assert bus.subscriber_count == 1
    await stream.aclose()
    assert bus.subscriber_count == 0


async def test_event_stream_sends_a_heartbeat_when_nothing_happens():
    from hal_mary.web.app import event_stream

    bus = EventBus()
    stream = event_stream(bus, heartbeat_s=0.01)
    try:
        await anext(stream)
        beat = await anext(stream)
        assert beat.startswith(":")
    finally:
        await stream.aclose()


# --- what the templates are allowed to see -----------------------------------


def rendered_contexts(db_path: Path) -> dict[str, dict]:
    """The Jinja context every page was rendered with.

    Starlette's TestClient carries it back on the response, which is the only
    way to see what a template *could* have printed rather than what it did.
    """
    contexts = {}
    with client_for(db_path) as client:
        contexts["/login"] = client.get("/login").context
        login(client)
        for path in ("/status", "/team", "/league", "/draft"):
            contexts[path] = client.get(path).context
    assert all(context is not None for context in contexts.values())
    return contexts


def test_no_page_is_handed_the_settings_object(db_path: Path):
    """One ``{{ settings }}`` in a future partial would print live ESPN cookies.

    Nothing renders it today, which is exactly why it is worth pinning now: the
    diagnostics fragment that does render it will be written by someone who
    assumed the context was safe.
    """
    populate_league(db_path)
    for path, context in rendered_contexts(db_path).items():
        offenders = [key for key, value in context.items() if isinstance(value, Settings)]
        assert offenders == [], f"{path} hands the templates {offenders}"


def test_no_page_can_leak_a_secret_through_its_context(db_path: Path):
    """Belt and braces: no secret's *value* is reachable anywhere in a context."""
    populate_league(db_path)
    secrets_in_play = (FIXTURE_ENV["ESPN_S2"], FIXTURE_ENV["SWID"], PASSWORD)
    for path, context in rendered_contexts(db_path).items():
        reachable = " ".join(str(value) for value in context.values())
        for secret in secrets_in_play:
            assert secret not in reachable, f"{path} could render a secret"


# --- the look, and the LAN ---------------------------------------------------


def test_no_page_loads_anything_from_the_internet(db_path: Path):
    """It has to work on a LAN with no internet: nothing may come from a CDN."""
    populate_league(db_path)
    with client_for(db_path) as client:
        login(client)
        pages = [
            client.get(path).text
            for path in ("/login", "/status", "/team", "/league", "/draft")
        ]
    for text in pages:
        for marker in ("https://", "http://", "//cdn", "//unpkg"):
            assert marker not in text, f"page reaches off-box: {marker}"


def test_pages_are_phone_shaped(db_path: Path):
    with client_for(db_path) as client:
        login(client)
        text = client.get("/status").text
    assert 'name="viewport"' in text
    assert "/static/app.css" in text


def test_lan_url_uses_a_real_address_not_the_bind_address():
    from hal_mary.web.serve import lan_url

    url = lan_url("0.0.0.0", 8080, resolve=lambda: "192.168.1.184")
    assert url == "http://192.168.1.184:8080"


def test_lan_url_keeps_an_explicit_host():
    from hal_mary.web.serve import lan_url

    assert lan_url("127.0.0.1", 8080, resolve=lambda: "192.168.1.184") == "http://127.0.0.1:8080"


def test_lan_url_falls_back_when_the_address_cannot_be_found():
    from hal_mary.web.serve import lan_url

    def unresolvable():
        raise OSError("no route to host")

    assert "8080" in lan_url("0.0.0.0", 8080, resolve=unresolvable)


def test_serve_refuses_without_a_password(capsys, monkeypatch, db_path: Path):
    from hal_mary import cli

    monkeypatch.setattr(cli, "load_cli_settings", lambda: make_settings(db_path, WEB_PASSWORD=None))
    assert cli.main(["serve"]) == cli.EXIT_NOT_CONFIGURED
    assert "WEB_PASSWORD" in capsys.readouterr().err


def test_serve_prints_the_lan_url_and_starts_uvicorn(capsys, monkeypatch, db_path: Path):
    from hal_mary import cli
    from hal_mary.web import serve as serve_module

    started: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_cli_settings", lambda: make_settings(db_path))
    monkeypatch.setattr(serve_module, "outbound_ip", lambda: "192.168.1.184")
    monkeypatch.setattr(serve_module, "run_server", lambda **kwargs: started.update(kwargs) or None)
    assert cli.main(["serve"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "http://192.168.1.184:8080" in out
    assert ".env" in out
    assert started["host"] == "0.0.0.0"
    assert started["port"] == 8080
    assert started["reload"] is False


def test_serve_reload_flag_is_passed_through(monkeypatch, db_path: Path):
    from hal_mary import cli
    from hal_mary.web import serve as serve_module

    started: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_cli_settings", lambda: make_settings(db_path))
    monkeypatch.setattr(serve_module, "outbound_ip", lambda: "192.168.1.184")
    monkeypatch.setattr(serve_module, "run_server", lambda **kwargs: started.update(kwargs) or None)
    assert cli.main(["serve", "--reload"]) == cli.EXIT_OK
    assert started["reload"] is True


# --- the age helper ----------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (5, "just now"),
        (90, "1 minute ago"),
        (60 * 5, "5 minutes ago"),
        (60 * 60, "1 hour ago"),
        (60 * 60 * 3, "3 hours ago"),
        (60 * 60 * 26, "1 day ago"),
        (60 * 60 * 24 * 3, "3 days ago"),
    ],
)
def test_age_in_words(seconds: int, expected: str):
    from datetime import UTC, datetime, timedelta

    from hal_mary.web.app import age_in_words

    now = datetime.now(UTC)
    stamp = (now - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    assert age_in_words(stamp, now=now) == expected


def test_age_in_words_handles_nonsense():
    from hal_mary.web.app import age_in_words

    assert age_in_words(None) == "never"
    assert age_in_words("not-a-timestamp") == "unknown"


# --- /status: the resolved paths ---------------------------------------------
#
# The bug this reports on: a relative memory_dir resolved against the process
# working directory found nothing, standing_memory() returned "", and every
# prompt went out without its standing context. Nothing crashed and nothing was
# logged where anyone would see it. The status page is where an operator looks
# when the advice is off, so it has to be able to say "I am looking here, and
# there is nothing there".


def test_status_shows_every_resolved_path(db_path: Path):
    settings = make_settings(db_path)
    with client_for(db_path, settings=settings) as client:
        login(client)
        text = client.get("/status").text

    assert str(settings.paths.memory_dir) in text
    assert str(settings.paths.prompts_dir) in text
    assert str(settings.claude.system_prompt_file) in text
    assert str(settings.config_path) in text


def test_status_calls_a_missing_memory_directory_a_problem(db_path: Path, tmp_path: Path):
    """Named in the red box, with the path it actually looked at."""
    absent = tmp_path / "no-such-memory"
    settings = make_settings(db_path)
    settings = settings.model_copy(
        update={"paths": settings.paths.model_copy(update={"memory_dir": absent})}
    )

    with client_for(db_path, settings=settings) as client:
        login(client)
        text = client.get("/status").text

    assert str(absent) in text
    assert "does not exist" in text
    assert "Problems right now" in text


def test_status_distinguishes_an_empty_memory_directory_from_a_missing_one(
    db_path: Path, tmp_path: Path
):
    """"There are no notes" reads differently from "I am looking in the wrong
    place". Both are worth saying; saying the same thing for both is the bug."""
    empty = tmp_path / "empty-memory"
    empty.mkdir()
    settings = make_settings(db_path)
    settings = settings.model_copy(
        update={"paths": settings.paths.model_copy(update={"memory_dir": empty})}
    )

    with client_for(db_path, settings=settings) as client:
        login(client)
        text = client.get("/status").text

    assert "does not exist" not in text
    assert "no standing-memory" in text.lower()


def test_status_is_quiet_about_paths_that_are_all_there(db_path: Path):
    """The repo's own config: nothing about paths belongs in the red box."""
    with client_for(db_path) as client:
        login(client)
        text = client.get("/status").text

    assert "does not exist" not in text
    assert "no standing-memory" not in text.lower()
