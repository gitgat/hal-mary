"""The FastAPI application: auth, the pages, and the live event stream.

Everything here is built for one reader on one phone. Caroline knows the rules
of football and nothing else, she is reading this next to ESPN's own app, and
sometimes there is a 90-second pick clock running. So: short sentences, big
targets, and a page that says what is wrong rather than showing a wall of green
ticks.

Three things about the shape of this module are deliberate.

**Connections, not a connection.** ``create_app`` takes ``connect``, a factory,
and every request opens and closes its own ``sqlite3.Connection``. A single
shared connection cannot be used from another thread — ``sqlite3`` raises — and
the app's handlers do not all run on the thread that built the app: uvicorn's
loop, Starlette's ``TestClient`` portal and the ``asyncio.to_thread`` worker that
runs a sync are three different threads. Opening a local SQLite file is
microseconds; a connection that crosses a thread boundary is a crash on draft
morning. This is also the injection seam the tests use: point ``connect`` at a
temporary database and nothing touches ``hal.db``.

**Auth is structural, not conditional.** Protected routes live on a router that
carries the session dependency, so a new page is authenticated by construction
rather than by remembering to check. ``/login``, ``/healthz`` and ``/static``
are the only routes on the public router, and a test walks every route in the
app to prove nothing else escaped.

**Handlers are ``async`` and read SQLite inline.** FastAPI runs ``def``
handlers in a thread pool, which would put the reads on an arbitrary thread; the
reads are sub-millisecond local file reads, so they run on the loop. Anything
genuinely slow — the ESPN auth check, a sync — goes to ``asyncio.to_thread``
explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from hal_mary import db
from hal_mary.config import Settings
from hal_mary.espn.sync import last_sync
from hal_mary.memory import standing_memory_files

__all__ = [
    "EventStreamResponse",
    "LoginLimiter",
    "MissingPasswordError",
    "age_in_words",
    "create_app",
    "event_stream",
    "session_key",
]

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

#: What the signed cookie carries. The value is not a secret and not a user id:
#: there is one password and one person. What matters is that it is signed with
#: a key derived from the password, so changing the password ends every session.
SESSION_VALUE = "authenticated"
SESSION_SALT = "hal-mary.web.session.v1"

#: scrypt cost for the session key. Not in config.toml: it is not a tunable of
#: behaviour, and a box that quietly lowered it would weaken the only thing
#: standing between a captured cookie and the household password. n=2**14 with
#: r=8 is ~16 MB and tens of milliseconds — paid once per process, since the
#: derivation is cached, and it makes offline guessing about a hundred thousand
#: times slower than the single SHA-256 this used to be.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

#: PBKDF2 rounds for the fallback below.
PBKDF2_ROUNDS = 600_000

#: The syncs whose age the status page reports, and what to call them.
SYNC_KINDS = (("league", "League and rosters"), ("draft", "Draft picks"))

#: Row counts worth showing, in the order they matter to someone diagnosing a
#: quiet application: did ESPN load, did research run, is there anything to say.
COUNTED_TABLES = (
    ("Teams", "teams"),
    ("Players", "players"),
    ("Board", "board"),
    ("Notes", "notes"),
    ("Advice", "advice"),
)

#: Lineup slots in the order a roster is read, not the order ESPN returns them.
#: Anything ESPN sends that is not listed here is appended, so an unfamiliar
#: slot shows up rather than vanishing.
SLOT_ORDER = ("QB", "RB", "WR", "TE", "RB/WR/TE", "WR/TE", "OP", "D/ST", "K", "BE", "IR")

#: Slot names as Caroline would say them.
SLOT_LABELS = {
    "QB": "Quarterback",
    "RB": "Running back",
    "WR": "Wide receiver",
    "TE": "Tight end",
    "RB/WR/TE": "Flex",
    "WR/TE": "Flex",
    "OP": "Flex",
    "D/ST": "Defense",
    "K": "Kicker",
    "BE": "Bench",
    "IR": "Injured reserve",
}

#: Injury values ESPN sends that mean "nothing to see here".
HEALTHY = {"ACTIVE", "NORMAL", ""}


class MissingPasswordError(RuntimeError):
    """Raised when ``WEB_PASSWORD`` is unset and the app would start anyway."""


@functools.lru_cache(maxsize=8)
def session_key(password: str) -> str:
    """Derive the cookie signing key from the shared password.

    A memory-hard KDF, not a hash. The session cookie crosses the LAN in
    cleartext — there is no TLS on a home network — so anyone who captures one
    (a guest device, a phone backup, a router that logs) holds a value they can
    test password guesses against offline. Against a single SHA-256 that is
    billions of guesses a second on a laptop GPU, and "the password Bryan and
    Caroline picked" is not a passphrase that survives billions of guesses.
    scrypt makes each guess cost memory as well as time.

    Cached because the derivation is deliberately slow and the input never
    changes within a process: the app is built once per start, and the tests
    build dozens. The password is already in memory either way, so caching what
    is derived from it gives away nothing new.
    """
    try:
        derived = hashlib.scrypt(
            password.encode(),
            salt=SESSION_SALT.encode(),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
        )
    except (ValueError, MemoryError):  # pragma: no cover - needs an OpenSSL without scrypt
        # Some builds refuse scrypt's memory bound. PBKDF2 is weaker against
        # dedicated hardware but is still six orders of magnitude better than
        # the bare hash, and refusing to start would be worse than either.
        logger.warning("scrypt unavailable; deriving the session key with PBKDF2 instead")
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), SESSION_SALT.encode(), PBKDF2_ROUNDS, SCRYPT_DKLEN
        )
    return derived.hex()


class LoginLimiter:
    """Counts failed logins per client and locks that client out for a while.

    Online guessing is the other half of the problem the KDF only covers
    offline: without this, a device on the network can try the household
    password a few thousand times a second against `/login` and nothing
    anywhere would say so.

    Keyed per client rather than globally on purpose. A global counter would
    hand any device on the network a way to lock Caroline out of her own app
    thirty seconds before her pick — a denial of service dressed as a security
    control. Per-client is weaker (an attacker with several addresses gets
    several budgets) but it cannot be turned against her.

    State is in memory and per process: a restart forgives everyone. That is
    the right trade for a home LAN, where the alternative is a table to
    maintain and a lockout that survives the fix.
    """

    def __init__(
        self,
        max_attempts: int,
        lockout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._failures: dict[str, tuple[int, float]] = {}

    def locked_out(self, client: str) -> bool:
        """Is this client currently barred? Expired lockouts are forgotten."""
        record = self._failures.get(client)
        if record is None:
            return False
        count, last_seen = record
        if self._clock() - last_seen > self.lockout_seconds:
            del self._failures[client]
            return False
        return count >= self.max_attempts

    def record_failure(self, client: str) -> int:
        """Count one failure; return the running total for this client."""
        count, _ = self._failures.get(client, (0, 0.0))
        count += 1
        self._failures[client] = (count, self._clock())
        return count

    def forget(self, client: str) -> None:
        """Clear a client's history, which a successful login does."""
        self._failures.pop(client, None)


# --- small helpers ----------------------------------------------------------


def age_in_words(stamp: str | None, now: datetime | None = None) -> str:
    """How long ago ``stamp`` was, in words Caroline can read at a glance.

    ``"never"`` for nothing, ``"unknown"`` for something unparseable. Never a
    raw timestamp: "2026-09-07T20:13:54+00:00" answers a different question than
    "3 hours ago", and only one of them is useful on a phone.
    """
    if not stamp:
        return "never"
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = ((now or datetime.now(UTC)) - moment).total_seconds()
    if seconds < 0:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    if seconds >= 60:  # pragma: no cover - covered by the minute branch above
        return "1 minute ago"
    return "just now"


def _safe_next(raw: str | None, default: str = "/status") -> str:
    """Only ever redirect back into this app.

    ``//evil.example`` and ``https://evil.example`` are both open redirects, and
    an open redirect on the one page that takes a password is how a phished
    login gets its password.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return default
    return raw


def claude_binary_status(settings: Settings) -> tuple[bool, str]:
    """Is the ``claude`` binary present and executable?

    Deliberately does **not** run it. Spawning it costs a second on a page that
    gets reloaded, and the test suite must never spawn the real one.
    """
    binary = settings.claude.binary
    resolved = shutil.which(binary)
    if resolved is None:
        candidate = Path(binary)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = str(candidate)
    if resolved is None:
        return False, f"The claude binary ({binary}) is not on the PATH — no research can run."
    return True, f"claude found at {resolved}"


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    """Count a table, or ``None`` if it cannot be read.

    An unmigrated or half-written database must still render the status page:
    that page is what someone opens *because* the database looks wrong.
    """
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []


# --- the event stream --------------------------------------------------------


async def event_stream(bus: Any, heartbeat_s: float) -> AsyncIterator[str]:
    """Server-sent events, framed, with a heartbeat and a guaranteed unsubscribe.

    ``EventBus.subscribe()`` is an async context manager and is used as one:
    ``close()`` has to happen on the loop that owns the subscription, and the
    context manager is what guarantees it runs when the generator is closed —
    which is what happens when a phone sleeps, the socket drops and Starlette
    tears the response down. Without it, every screen-lock would leak a
    subscription and the bus would fan out to a hundred dead queues by midnight.

    The heartbeat is a comment frame. It exists because a stream that says
    nothing for a minute is closed by phone browsers and by anything proxying in
    between, and a draft page that silently stopped updating is worse than one
    that never worked.
    """
    async with bus.subscribe() as subscription:
        # Sent immediately: it flushes the response headers, so the client knows
        # it is connected, and it proves the subscription is registered before
        # anybody waits on an event.
        yield ": connected\n\n"
        while True:
            try:
                event, payload = await asyncio.wait_for(
                    subscription.__anext__(), timeout=heartbeat_s
                )
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            except StopAsyncIteration:
                # The bus closed this subscription; ending the generator here is
                # not the same as raising, which asyncio would turn into a
                # RuntimeError inside an async generator.
                return
            yield f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


# --- the application ---------------------------------------------------------


class _NotAuthenticated(Exception):
    """Raised by the session dependency; handled into a redirect to /login."""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


class EventStreamResponse(StreamingResponse):
    """A streaming response that closes its generator when the client vanishes.

    Starlette ends a stream by cancelling the task that iterates it, which
    leaves the async generator suspended rather than closed; Python gets around
    to finalising it whenever the garbage collector does. For an ordinary
    response that is invisible. For this one it is the leak the brief is about:
    the generator holds an ``EventBus`` subscription, a phone that locks its
    screen disconnects, and a night of that is a bus fanning out to a hundred
    dead queues.

    So the close is made explicit. ``aclose()`` throws ``GeneratorExit`` into the
    generator, which unwinds through the ``async with bus.subscribe()`` block and
    unsubscribes — synchronously, so it completes even inside the cancelled
    scope it is running in.
    """

    async def stream_response(self, send: Any) -> None:
        try:
            await super().stream_response(send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def create_app(
    settings: Settings,
    connect: Callable[[], sqlite3.Connection] | None = None,
    *,
    bus: Any | None = None,
    check_auth: Callable[[], tuple[bool, str]] | None = None,
    run_sync: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    """Build the app.

    ``connect`` opens a database connection (default: the configured
    ``db_path``); ``bus`` is the :class:`~hal_mary.events.EventBus` ``/events``
    streams from; ``check_auth`` answers "are the ESPN cookies still good"
    (default: a real ``EspnClient`` call); ``run_sync`` performs a sync (default:
    a real one, opening its own connection inside the worker thread). Every one
    of them is injected so the tests build a whole app that reaches nothing.

    Raises :class:`MissingPasswordError` when ``WEB_PASSWORD`` is unset. That is
    not defensive politeness: this binds every interface on a home network and
    the database it serves holds live ESPN session cookies.
    """
    password = (settings.web_password or "").strip()
    if not password:
        raise MissingPasswordError(
            "WEB_PASSWORD is not set, so the web app would serve Caroline's ESPN "
            "session cookies to anyone on the network. Set WEB_PASSWORD in .env "
            "(see .env.example) and start it again."
        )

    open_conn = connect or (lambda: db.connect(settings.db_path))
    if bus is None:
        from hal_mary.events import EventBus

        bus = EventBus()
    check_auth = check_auth or _default_check_auth(settings)
    run_sync = run_sync or _default_run_sync(settings)

    # The signing key is derived from the password rather than stored: there is
    # no second secret to manage, sessions survive a restart (Caroline is not
    # logged out because the box rebooted), and changing WEB_PASSWORD
    # invalidates every outstanding cookie, which is exactly what changing a
    # shared password should mean. See session_key for why it is a KDF.
    serializer = URLSafeTimedSerializer(session_key(password), salt=SESSION_SALT)
    max_age = settings.web.session_max_age_days * 86400
    limiter = LoginLimiter(
        max_attempts=settings.web.login_max_attempts,
        lockout_seconds=settings.web.login_lockout_seconds,
    )
    # One sync at a time. Two taps mean two ESPN reads and two SQLite writers.
    sync_lock = asyncio.Lock()

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals["age_in_words"] = age_in_words

    app = FastAPI(title="hal-mary", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.bus = bus

    auth_cache: dict[str, Any] = {}

    @contextlib.contextmanager
    def database() -> Iterator[sqlite3.Connection]:
        conn = open_conn()
        try:
            yield conn
        finally:
            conn.close()

    def is_authenticated(request: Request) -> bool:
        token = request.cookies.get(settings.web.session_cookie)
        if not token:
            return False
        try:
            value = serializer.loads(token, max_age=max_age)
        except (BadSignature, SignatureExpired):
            return False
        return value == SESSION_VALUE

    def require_session(request: Request) -> None:
        if not is_authenticated(request):
            raise _NotAuthenticated(request.url.path)

    @app.exception_handler(_NotAuthenticated)
    async def _login_redirect(request: Request, exc: _NotAuthenticated) -> RedirectResponse:
        return RedirectResponse(f"/login?next={exc.next_url}", status_code=302)

    def page(
        request: Request, name: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        """Render a template with exactly the values it needs.

        The whole ``Settings`` object used to go in here. Nothing rendered it,
        which is precisely why it was dangerous: ``espn_s2``, ``swid`` and
        ``web_password`` are plain strings on it, so the first diagnostics
        partial that did ``{{ settings }}`` — written by someone who reasonably
        assumed the context was safe — would have printed Caroline's live ESPN
        session into a web page. Pages get fields, never the object.

        ``status_code`` is an explicit parameter because passing it as context
        silently rendered a 200: a failed login answered "OK" with a form
        saying otherwise.
        """
        return templates.TemplateResponse(
            request, name, {"nav": name, **context}, status_code=status_code
        )

    async def espn_auth() -> tuple[bool, str]:
        """The cached ESPN auth check.

        Cached because it is a network call on the page most likely to be
        reloaded three times in a row by someone trying to work out what is
        broken, and hammering an unofficial endpoint is how cookies get
        rate-limited at the worst moment.
        """
        now = time.monotonic()
        if auth_cache and now - auth_cache["at"] < settings.web.auth_check_seconds:
            return auth_cache["ok"], auth_cache["reason"]
        try:
            ok, reason = await asyncio.to_thread(check_auth)
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately everything. This is the page someone opens *because*
            # something is broken; an unexpected exception out of an unofficial
            # API client must appear on it as a sentence, not as a 500 that
            # hides every other check on the page.
            ok, reason = False, f"The ESPN check itself failed: {exc}"
        auth_cache.update(at=now, ok=ok, reason=reason)
        return ok, reason

    public = APIRouter()
    private = APIRouter(dependencies=[Depends(require_session)])

    # -- public ------------------------------------------------------------

    @public.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness only. No password, no database, no ESPN: it answers "is the
        process up", which is the one question a watchdog should ask."""
        return JSONResponse({"status": "ok"})

    @public.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/status") -> HTMLResponse:
        if is_authenticated(request):
            return RedirectResponse(_safe_next(next), status_code=302)
        return page(request, "login.html", next=_safe_next(next), error=None)

    @public.post("/login")
    async def login_submit(
        request: Request,
        password_input: str = Form("", alias="password"),
        next: str = Form("/status"),
    ) -> Any:
        client = request.client.host if request.client else "unknown"

        if limiter.locked_out(client):
            # Refused without even comparing: a limit that still checks the
            # password is only slowing down someone who was going to guess it.
            logger.warning("login refused: %s is locked out after too many attempts", client)
            return page(
                request,
                "login.html",
                status_code=429,
                next=_safe_next(next),
                error="Too many attempts. Wait a minute and try again.",
            )

        # compare_digest, not ==: string equality returns as soon as it finds a
        # differing byte, and this is reachable from every device on the LAN.
        if not secrets.compare_digest(password_input.encode(), password.encode()):
            count = limiter.record_failure(client)
            # The attempt itself is never logged, only that there was one: a log
            # file full of near-miss passwords is its own disclosure.
            logger.warning(
                "failed login from %s (%d of %d before lockout)",
                client,
                count,
                settings.web.login_max_attempts,
            )
            # One message for every failure. There is one password and one
            # person, so anything more specific only tells an attacker how close
            # they are.
            return page(
                request,
                "login.html",
                status_code=401,
                next=_safe_next(next),
                error="That password is not right.",
            )

        limiter.forget(client)
        logger.info("login from %s", client)
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(
            settings.web.session_cookie,
            serializer.dumps(SESSION_VALUE),
            max_age=max_age,
            httponly=True,
            samesite="lax",
            # Only over TLS. Set unconditionally, the browser would refuse to
            # send it back over plain HTTP on the LAN and login would appear to
            # succeed and then silently fail.
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    # -- pages -------------------------------------------------------------

    @private.post("/logout")
    async def logout() -> RedirectResponse:
        """Guarded, like every other page.

        Logging out is harmless in itself, which is why it is easy to leave
        public — but a public one means any page open on any device on the
        network can log Caroline out with a hidden form post, thirty seconds
        before her pick.
        """
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.web.session_cookie, path="/")
        return response

    @private.get("/")
    async def index() -> RedirectResponse:
        """7b repoints this at the draft page; until then the status page is
        the most useful thing to land on."""
        return RedirectResponse("/status", status_code=302)

    @private.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> HTMLResponse:
        auth_ok, auth_reason = await espn_auth()
        with database() as conn:
            context = _status_context(conn, settings, auth_ok, auth_reason)
        return page(request, "status.html", **context)

    @private.get("/team", response_class=HTMLResponse)
    async def team_page(request: Request) -> HTMLResponse:
        with database() as conn:
            context = _team_context(conn, settings)
        return page(request, "team.html", **context)

    @private.get("/league", response_class=HTMLResponse)
    async def league_page(request: Request) -> HTMLResponse:
        with database() as conn:
            context = _league_context(conn, settings)
        return page(request, "league.html", **context)

    @private.post("/sync")
    async def sync_now(request: Request) -> Any:
        """Pull ESPN again, from the browser.

        ``to_thread`` because a sync is seconds of blocking HTTP and SQLite
        writes; on the event loop it would freeze every other request, including
        the draft page's event stream.

        One at a time. ``hx-disabled-elt`` greys out the button on the HTMX
        path and nothing else: a double tap on a phone, a second device, or the
        no-JavaScript fallback would otherwise put two writers into SQLite and
        fire a second round of requests at an endpoint that rate-limits the
        cookies everything else depends on.
        """
        if sync_lock.locked():
            return _sync_response(
                request, None, None, notice="A sync is already running — give it a moment."
            )

        try:
            async with sync_lock:
                summary = await asyncio.to_thread(run_sync)
            error = None
        except Exception as exc:  # noqa: BLE001 - a failed sync is a message, not a 500
            # sync_league raises EspnError, sqlite3.Error, or whatever a
            # surprising ESPN payload produces. All of them mean the same thing
            # to the person who pressed the button, and all of them belong in
            # the fragment under it rather than in a traceback she cannot see.
            summary, error = None, f"{type(exc).__name__}: {exc}"
        else:
            bus.publish("synced", dict(summary or {}))

        return _sync_response(request, summary, error)

    def _sync_response(
        request: Request,
        summary: dict[str, Any] | None,
        error: str | None,
        notice: str | None = None,
    ) -> Any:
        """The fragment for HTMX, a redirect for a plain form post.

        ``notice`` is not ``error``: "a sync is already running" is the app
        working, and telling Caroline her sync *failed* when it did not is how
        someone ends up jabbing the button through a draft.
        """
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                request,
                "partials/sync_result.html",
                {
                    "summary": summary,
                    "error": error,
                    "notice": notice,
                    "when": age_in_words(db.utc_now()),
                },
            )
        return RedirectResponse("/status", status_code=303)

    @private.get("/events", response_class=EventStreamResponse)
    async def events() -> StreamingResponse:
        return EventStreamResponse(
            event_stream(bus, settings.web.sse_heartbeat_s),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # nginx buffers text/event-stream into uselessness otherwise.
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(public)
    app.include_router(private)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


# --- page data ---------------------------------------------------------------


def _status_context(
    conn: sqlite3.Connection, settings: Settings, auth_ok: bool, auth_reason: str
) -> dict[str, Any]:
    """Everything the status page shows, and the blunt list of what is wrong.

    Problems are collected separately from the detail rows on purpose. A page
    that shows twenty green ticks and one red one is a page whose red one gets
    missed.
    """
    problems: list[str] = []

    missing = settings.missing_secrets()
    if missing:
        problems.append(
            f"Missing from .env: {', '.join(missing)}. Nothing that needs them can run."
        )
    if not auth_ok:
        problems.append(auth_reason)

    claude_ok, claude_reason = claude_binary_status(settings)
    if not claude_ok:
        problems.append(claude_reason)

    # Configured paths, resolved. This block exists because the failure it
    # reports has no other symptom: a memory_dir resolved against the wrong
    # directory does not crash anything, it just strips the standing context out
    # of every prompt and makes the advice quietly worse. Two different
    # sentences on purpose — "there are no notes" and "I am looking in the wrong
    # place" are different problems with different fixes.
    memory_dir = settings.paths.memory_dir
    if not memory_dir.is_dir():
        problems.append(
            f"The standing-memory directory {memory_dir} does not exist, so every "
            f"prompt is going out without the context that says who Caroline is "
            f"and what the league's rules are. Check paths.memory_dir in "
            f"{settings.config_path}."
        )
    elif not standing_memory_files(settings):
        problems.append(
            f"{memory_dir} holds no standing-memory notes, so every prompt is "
            f"going out without the context that says who Caroline is and what "
            f"the league's rules are."
        )

    syncs = []
    for kind, label in SYNC_KINDS:
        row = last_sync(conn, kind)
        stamp = (row["finished_at"] or row["started_at"]) if row else None
        status = row["status"] if row else "never run"
        age = age_in_words(stamp)
        syncs.append(
            {
                "kind": kind,
                "label": label,
                "age": age,
                "status": status,
                "error": row["error"] if row else None,
                "ok": bool(row) and status == "ok",
            }
        )
        if row is None:
            problems.append(f"{label} have never been synced from ESPN.")
        elif status == "error":
            problems.append(f"The last {label.lower()} sync failed {age}: {row['error']}")

    return {
        "problems": problems,
        "espn": {"ok": auth_ok, "reason": auth_reason},
        "claude": {"ok": claude_ok, "reason": claude_reason},
        "missing_secrets": missing,
        "syncs": syncs,
        "counts": [(label, _count(conn, table)) for label, table in COUNTED_TABLES],
        "jobs": _all(
            conn,
            "SELECT job, started_at, finished_at, status, summary, error"
            " FROM job_runs ORDER BY id DESC LIMIT 10",
        ),
        "db_path": str(settings.db_path),
        "paths": [
            {"label": label, "path": str(path), "ok": exists}
            for label, path, exists in settings.resolved_paths()
        ],
    }


def _roster_slot_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = _one(conn, "SELECT roster_slots_json FROM league_settings WHERE id = 1")
    if row is None or not row["roster_slots_json"]:
        return {}
    try:
        raw = json.loads(row["roster_slots_json"])
    except json.JSONDecodeError:
        return {}
    return {str(k): int(v) for k, v in raw.items() if v}


def _slot_sort_key(slot: str) -> tuple[int, str]:
    return (SLOT_ORDER.index(slot) if slot in SLOT_ORDER else len(SLOT_ORDER), slot)


def _team_context(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Caroline's roster, grouped by slot, with open slots as empty rows.

    The open rows are the point of the page. "No quarterback yet" has to be
    visible without counting, because during a draft nobody counts.
    """
    team_id = settings.team_id
    team = _one(conn, "SELECT * FROM teams WHERE team_id = ?", (team_id,))
    rows = (
        _all(
            conn,
            """
            SELECT p.name, p.position, p.pro_team, p.injury_status, r.slot, b.bye_week
              FROM roster_slots r
              JOIN players p ON p.player_id = r.player_id
              LEFT JOIN board b ON b.player_id = r.player_id
             WHERE r.team_id = ?
             ORDER BY p.name
            """,
            (team_id,),
        )
        if team_id is not None
        else []
    )

    configured = _roster_slot_counts(conn)
    players_by_slot: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        slot = row["slot"] or row["position"] or "Other"
        players_by_slot.setdefault(slot, []).append(
            {
                "name": row["name"],
                "position": row["position"],
                "pro_team": row["pro_team"],
                "bye_week": row["bye_week"],
                "injury": (row["injury_status"] or "").upper(),
                "hurt": (row["injury_status"] or "").upper() not in HEALTHY,
            }
        )

    groups = []
    for slot in sorted(set(configured) | set(players_by_slot), key=_slot_sort_key):
        filled = players_by_slot.get(slot, [])
        open_slots = max(0, configured.get(slot, len(filled)) - len(filled))
        groups.append(
            {
                "slot": slot,
                "label": SLOT_LABELS.get(slot, slot),
                "players": filled,
                "open": open_slots,
            }
        )

    return {
        "team": team,
        "team_id": team_id,
        "groups": groups,
        "drafted": len(rows),
        "league_synced": _one(conn, "SELECT 1 FROM league_settings WHERE id = 1") is not None,
    }


def _league_context(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    league = _one(conn, "SELECT * FROM league_settings WHERE id = 1")
    teams = _all(
        conn,
        "SELECT team_id, name, owner, abbrev, draft_slot FROM teams"
        " ORDER BY CASE WHEN draft_slot IS NULL THEN 1 ELSE 0 END, draft_slot, team_id",
    )
    return {"league": league, "teams": teams, "my_team_id": settings.team_id}


# --- the real-world defaults --------------------------------------------------


def _default_check_auth(settings: Settings) -> Callable[[], tuple[bool, str]]:
    """The live ESPN cookie check, run in a worker thread by the caller."""

    def check() -> tuple[bool, str]:
        from hal_mary.espn import EspnClient

        return EspnClient(settings).check_auth()

    return check


def _default_run_sync(settings: Settings) -> Callable[[], dict[str, Any]]:
    """A real sync, for the button on the status page.

    The connection is opened **inside** this function because it runs on a
    worker thread: a ``sqlite3.Connection`` belongs to the thread that created
    it, so one made on the event loop and used here would raise.
    """

    def run() -> dict[str, Any]:
        from hal_mary.espn import EspnClient, sync_draft, sync_league

        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
            client = EspnClient(settings)
            summary = dict(sync_league(conn, client))
            summary["picks"] = len(sync_draft(conn, client))
            return summary
        finally:
            conn.close()

    return run
