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
from starlette.routing import Route

from hal_mary import chat as chat_engine
from hal_mary import db
from hal_mary.config import Settings
from hal_mary.draft import loop as draft_loop
from hal_mary.draft import store as draft_store
from hal_mary.espn.sync import last_sync
from hal_mary.mcp.server import MCP_PATH, build_endpoint
from hal_mary.memory import standing_memory_files
from hal_mary.web.chat_page import Answering, chat_context, chat_event_stream
from hal_mary.web.draft_page import draft_context
from hal_mary.web.positions import SLOT_LABELS, position_word, slot_sort_key

__all__ = [
    "CSRF_FIELD",
    "SAFE_METHODS",
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

#: Injury values ESPN sends that mean "nothing to see here".
HEALTHY = {"ACTIVE", "NORMAL", ""}

#: The hidden form field carrying the CSRF token, and the methods that do not
#: need one. Anything not listed changes state and is checked.
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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


class _CsrfFailed(Exception):
    """Raised by the CSRF dependency; handled into a plain 403.

    Not a redirect. A redirect would send a forged post round the loop again,
    and the honest answer to "this request did not come from a hal-mary page" is
    to refuse it and say so.
    """


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
    make_runner: Callable[[Settings, sqlite3.Connection], Any] | None = None,
) -> FastAPI:
    """Build the app.

    ``connect`` opens a database connection (default: the configured
    ``db_path``); ``bus`` is the :class:`~hal_mary.events.EventBus` ``/events``
    streams from; ``check_auth`` answers "are the ESPN cookies still good"
    (default: a real ``EspnClient`` call); ``run_sync`` performs a sync (default:
    a real one, opening its own connection inside the worker thread);
    ``make_runner`` builds the :class:`~hal_mary.claude_runner.ClaudeRunner` the
    chat page streams from, and is handed the connection its own worker thread
    opened. Every one of them is injected so the tests build a whole app that
    reaches nothing.

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
    make_runner = make_runner or _default_make_runner
    # One live chat answer per conversation, for this app. See chat_page.
    answering = Answering()

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

    # The MCP endpoint is built before the app because its session manager needs
    # a lifespan, and FastAPI takes that at construction. It is a separate door
    # with a separate key: see hal_mary.mcp.server.
    mcp = build_endpoint(settings, open_conn)
    app = FastAPI(
        title="hal-mary",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=mcp.lifespan,
    )
    app.state.settings = settings
    app.state.bus = bus
    app.state.mcp_enabled = mcp.enabled

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

    async def require_csrf(request: Request) -> None:
        """Double-submit: the form must echo the token in the cookie.

        7a shipped without this because its only POST was a sync, whose worst
        case was an extra read of ESPN. Manual pick entry is the first genuinely
        state-changing POST — anything on the house network, or any page open in
        another tab, could otherwise post a pick into her draft — so the check
        lands on the router rather than on the handlers. A page added to the
        private router is protected by construction, the same way it is
        authenticated by construction.

        The token is read from a header first so an HTMX request can carry it
        without a form, then from the body. ``request.form()`` caches its result
        on the request, so parsing it here costs the handler nothing.
        """
        if request.method in SAFE_METHODS:
            return
        cookie = request.cookies.get(settings.web.csrf_cookie) or ""
        sent = request.headers.get(CSRF_HEADER) or ""
        if not sent:
            try:
                form = await request.form()
            except Exception:  # noqa: BLE001 - a body we cannot parse has no token
                form = {}
            sent = str(form.get(CSRF_FIELD) or "")
        # compare_digest, not ==: reachable from every device on the LAN.
        if not cookie or not secrets.compare_digest(sent, cookie):
            logger.warning(
                "refused %s %s: the CSRF token was missing or did not match",
                request.method,
                request.url.path,
            )
            raise _CsrfFailed

    @app.middleware("http")
    async def issue_csrf_cookie(request: Request, call_next: Any) -> Any:
        """Make sure every response carries a token she can submit back.

        Issued here rather than at login because ``/login`` itself is a form:
        the cookie has to exist before the first page is rendered, and it has to
        survive logging out and back in. It is not a secret and not tied to the
        session — it only has to be unguessable by another origin, which cannot
        read it.
        """
        token = request.cookies.get(settings.web.csrf_cookie)
        fresh = not token
        if fresh:
            token = secrets.token_urlsafe(32)
        request.state.csrf_token = token
        response = await call_next(request)
        if fresh:
            response.set_cookie(
                settings.web.csrf_cookie,
                token,
                max_age=max_age,
                # HttpOnly because nothing needs to read it in the browser: the
                # token is rendered into each form server-side, so a script that
                # could read it would only be an XSS handed the keys.
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response

    @app.exception_handler(_NotAuthenticated)
    async def _login_redirect(request: Request, exc: _NotAuthenticated) -> RedirectResponse:
        return RedirectResponse(f"/login?next={exc.next_url}", status_code=302)

    @app.exception_handler(_CsrfFailed)
    async def _csrf_refused(request: Request, _exc: _CsrfFailed) -> HTMLResponse:
        return HTMLResponse(
            "<p>That did not come from a hal-mary page, so nothing was changed. "
            "Reload the page and try again.</p>",
            status_code=403,
        )

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
            request,
            name,
            {
                "nav": name,
                "csrf_token": getattr(request.state, "csrf_token", ""),
                **context,
            },
            status_code=status_code,
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
    private = APIRouter(dependencies=[Depends(require_session), Depends(require_csrf)])

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
        """The draft page is the one she opens on the night, so it is home."""
        return RedirectResponse("/draft", status_code=302)

    # -- the draft page ----------------------------------------------------

    def loop_error(request: Request) -> str | None:
        """Why the draft loop is not running, if it is not.

        ``start_draft_loop`` deliberately tolerates a loop that will not start,
        because serving the page matters more than the loop that feeds it. The
        cost of that is a page which would otherwise show frozen data with no
        sign anything is wrong, so the reason is read back off the app and put
        in front of her.
        """
        thread = getattr(request.app.state, "draft_loop", None)
        if thread is None or getattr(thread, "alive", False):
            return None
        return getattr(thread, "error", None)

    def loop_watching(request: Request) -> dict[str, Any] | None:
        """The running loop's own cadence, or ``None`` when there is no loop.

        Asked rather than derived, because the loop is the only thing that knows
        about the "The draft has started" override: between the draft opening
        and pick 1 it is on draft-night cadence and the board says nothing yet.
        """
        thread = getattr(request.app.state, "draft_loop", None)
        reader = getattr(thread, "watching", None)
        return reader() if reader is not None else None

    def page_context(request: Request, position: str = "") -> dict[str, Any]:
        with database() as conn:
            return draft_context(
                conn,
                settings,
                position=position,
                loop_error=loop_error(request),
                watching=loop_watching(request),
            )

    @private.get("/draft", response_class=HTMLResponse)
    async def draft(request: Request, position: str = "") -> HTMLResponse:
        return page(request, "draft.html", **page_context(request, position))

    @private.get("/draft/live", response_class=HTMLResponse)
    async def draft_live(request: Request, position: str = "") -> HTMLResponse:
        """The live half of the page, for a swap rather than a reload.

        A reload loses her scroll position and closes the manual-entry panel,
        which on a phone mid-draft is genuinely disruptive. The event listener
        and the ten-second fallback poll both land here.
        """
        return _fragment(
            request, "partials/draft_live.html", **page_context(request, position)
        )

    @private.post("/draft/pick")
    async def draft_pick(
        request: Request,
        player_name: str = Form(""),
        team_id: str = Form(""),
    ) -> Any:
        """Record a pick Caroline entered by hand.

        The lifeline if ESPN stops updating, and the whole no-ESPN contingency
        depends on it. It goes through the draft loop's own ``record_manual_pick``
        so a hand-entered pick and an ESPN one take exactly the same path — a
        second path is a second set of rules about who is gone.

        The connection is this request's own. The loop runs on another thread
        with another connection, and a ``sqlite3.Connection`` belongs to the
        thread that opened it.
        """
        name = (player_name or "").strip()
        if not name:
            return _pick_response(
                request, error="Type the player's name first — hal-mary needs a name."
            )

        try:
            owner = int(team_id) if str(team_id).strip() else None
        except ValueError:
            owner = None

        try:
            with database() as conn:
                outcome = draft_loop.record_manual_pick(
                    conn, player_name=name, team_id=owner, bus=bus
                )
        except Exception as exc:
            logger.exception("could not record a hand-entered pick")
            return _pick_response(
                request, error=f"That did not save ({type(exc).__name__}). Try again."
            )

        return _pick_response(
            request,
            name=name,
            already=not outcome.get("recorded"),
            unmatched=bool(outcome.get("unmatched")),
            # With no board, apply_new_picks crossed nobody off and reported no
            # unmatched picks either — so without this the fragment would claim
            # he is off a list that does not exist.
            board_missing=bool(outcome.get("board_missing")),
        )

    @private.post("/draft/started")
    async def draft_has_started(request: Request) -> Any:
        """"The draft has started" — the override, from the draft page.

        Three things, in this order, because the first is instant and the second
        is seconds of blocking HTTP:

        1. the draft loop goes to draft-night cadence *now* rather than at the
           end of an idle interval it may have just begun;
        2. a sync runs, which is what re-reads the order ESPN draws when the
           draft opens — the step ``docs/SETUP.md`` makes unconditional;
        3. it reports what it found: whether the drawn order has been read, and
           who it now believes is on the clock.

        **An override, not the mechanism.** The loop reaches draft-night cadence
        on its own from the first pick ESPN reports, so a night nobody presses
        this costs one idle interval rather than the draft. That is the whole
        reason it is safe for this to be a button.

        A failed sync is a sentence, not a 500, and it does not undo step 1:
        ESPN being briefly unreachable is the moment to be *more* attentive, not
        less.
        """
        switched = _tell_the_loop(request)

        summary: dict[str, Any] | None = None
        error: str | None = None
        notice: str | None = None
        if sync_lock.locked():
            # Not an error: a sync she started a moment ago is already doing the
            # one thing this button needed it for.
            notice = "A sync is already running — give it a moment."
        else:
            try:
                async with sync_lock:
                    summary = await asyncio.to_thread(run_sync)
            except Exception as exc:  # noqa: BLE001 - a failed sync is a message
                error = f"{type(exc).__name__}: {exc}"
            else:
                bus.publish("synced", dict(summary or {}))

        if not request.headers.get("hx-request"):
            return RedirectResponse("/draft", status_code=303)
        with database() as conn:
            context = draft_context(
                conn,
                settings,
                loop_error=loop_error(request),
                watching=loop_watching(request),
            )
        return _fragment(
            request,
            "partials/draft_started.html",
            switched=switched,
            error=error,
            notice=notice,
            **context,
        )

    def _tell_the_loop(request: Request) -> dict[str, Any] | None:
        """Put the draft loop on draft-night cadence. ``None`` when none runs.

        A box with no ESPN credentials has no loop at all, and the page has to
        say that rather than report a switch that did not happen.
        """
        thread = getattr(request.app.state, "draft_loop", None)
        starter = getattr(thread, "draft_started", None)
        if starter is None:
            return None
        try:
            return starter()
        except Exception:
            logger.exception("could not tell the draft loop the draft has started")
            return None

    @private.post("/draft/unmatched/resolve")
    async def resolve_unmatched(request: Request, unmatched_id: str = Form("")) -> Any:
        """Dismiss one "the board and ESPN disagree" warning.

        Dismissing means "I have dealt with this", not "try again":
        ``loop.pending_picks`` skips every filed row, resolved or not, so a
        resolved pick is never re-applied to the board. The template says so,
        because a button that quietly did nothing would be worse than no button.
        """
        try:
            row_id = int(unmatched_id)
        except (TypeError, ValueError):
            row_id = None
        if row_id is not None:
            with (
                database() as conn,
                contextlib.suppress(sqlite3.Error),
                db.transaction(conn),
            ):
                conn.execute(
                    "UPDATE unmatched_picks SET resolved_at = ?"
                    " WHERE id = ? AND resolved_at IS NULL",
                    (db.utc_now(), row_id),
                )
        if not request.headers.get("hx-request"):
            return RedirectResponse("/draft", status_code=303)
        with database() as conn:
            context = draft_context(conn, settings)
        return _fragment(request, "partials/unmatched.html", **context)

    def _fragment(request: Request, template: str, **context: Any) -> HTMLResponse:
        """A template rendered as a fragment, with the token every form needs.

        The parameter is ``template`` rather than ``name`` because the contexts
        passed through here carry a ``name`` of their own — a player's.
        """
        return templates.TemplateResponse(
            request,
            template,
            {"csrf_token": getattr(request.state, "csrf_token", ""), **context},
        )

    def _pick_response(request: Request, **outcome: Any) -> Any:
        if not request.headers.get("hx-request"):
            return RedirectResponse("/draft", status_code=303)
        return _fragment(request, "partials/pick_result.html", **outcome)

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

    # -- the chat page -----------------------------------------------------

    @private.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request, session: int | None = None) -> HTMLResponse:
        with database() as conn:
            context = chat_context(conn, settings, session)
        return page(request, "chat.html", **context)

    @private.post("/chat/new")
    async def chat_new() -> RedirectResponse:
        with database() as conn:
            session_id = chat_engine.start_session(conn)
        return RedirectResponse(f"/chat?session={session_id}", status_code=303)

    @private.post("/chat/send")
    async def chat_send(
        request: Request,
        session_id: str = Form(""),
        message: str = Form(""),
    ) -> Any:
        """Record the question. The answer is streamed from ``/chat/stream``.

        Two routes rather than one because the answer takes up to a minute and
        the question must survive that: a page reloaded in the middle finds an
        unanswered question and reattaches to it, rather than losing what she
        typed. The POST is therefore fast and boring, which is also what lets it
        be an ordinary CSRF-checked form post.
        """
        text = (message or "").strip()
        if not text:
            return _chat_response(
                request, None, error="Type a question first — the box is empty."
            )

        with database() as conn:
            here = _session_or_new(conn, session_id)
            try:
                chat_engine.record_question(conn, here, text)
            except Exception as exc:
                logger.exception("could not save a chat question")
                return _chat_response(
                    request, here, error=f"That did not save ({type(exc).__name__})."
                )
            asked = chat_engine.get_messages(conn, here)[-1]
        return _chat_response(request, here, message=asked)

    def _session_or_new(conn: sqlite3.Connection, raw: str) -> int:
        """The conversation she is in, starting one if she is not in any.

        She should be able to open the page and type. Making "new conversation"
        a step before the first question is a step that exists for the database's
        benefit and nobody else's.
        """
        try:
            here = int(str(raw).strip())
        except (TypeError, ValueError):
            here = 0
        if here and chat_engine.get_session(conn, here) is not None:
            return here
        return chat_engine.start_session(conn)

    def _chat_response(
        request: Request,
        session_id: int | None,
        *,
        message: Any = None,
        error: str | None = None,
    ) -> Any:
        if not request.headers.get("hx-request"):
            target = f"/chat?session={session_id}" if session_id else "/chat"
            return RedirectResponse(target, status_code=303)
        return _fragment(
            request,
            "partials/chat_turn.html",
            session_id=session_id,
            message=message,
            error=error,
            pending=message is not None,
        )

    @private.get("/chat/stream/{session_id}", response_class=EventStreamResponse)
    async def chat_stream(session_id: int) -> StreamingResponse:
        """The answer, a token at a time.

        ``EventStreamResponse`` rather than a plain one: Starlette ends a stream
        by cancelling the task iterating it, which leaves the generator suspended
        rather than closed, and this generator's close is what tells its worker
        the phone has gone.
        """
        return EventStreamResponse(
            chat_event_stream(
                open_conn,
                settings,
                make_runner,
                answering,
                session_id,
                settings.web.sse_heartbeat_s,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @private.post("/chat/remember")
    async def chat_remember(
        request: Request,
        session_id: str = Form(""),
        text: str = Form(""),
    ) -> Any:
        """Save something the conversation established, as a note.

        This is the only way a fact reaches the rest of hal-mary from here: notes
        written with ``source_job='chat'`` are retrieved by the same search every
        research job reads from, so "I am away in week 11" is in front of the
        model the next time it writes a line-up.
        """
        try:
            here: int | None = int(str(session_id).strip())
        except (TypeError, ValueError):
            here = None

        saved, problem = False, None
        try:
            with database() as conn:
                chat_engine.remember(conn, here, text)
            saved = True
        except ValueError:
            problem = "There was nothing to save — highlight or type the fact first."
        except Exception as exc:
            logger.exception("could not save a note from the chat page")
            problem = f"That did not save ({type(exc).__name__}). Try again."

        if not request.headers.get("hx-request"):
            target = f"/chat?session={here}" if here else "/chat"
            return RedirectResponse(target, status_code=303)
        return _fragment(
            request, "partials/chat_note.html", saved=saved, error=problem
        )

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
    # Deliberately on neither router. `/mcp` carries its own bearer token and
    # must not accept the session cookie; the dashboard must not accept the MCP
    # token. Two doors, two keys — one of these is exposed through a tunnel and
    # the other is LAN-only. It is a Starlette Route rather than a mount so that
    # `/mcp` matches exactly, with no trailing-slash redirect for a client to
    # follow on a POST that carries a body.
    app.router.routes.append(
        Route(MCP_PATH, endpoint=mcp.asgi, methods=["GET", "POST", "DELETE"])
    )
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
        # What Cowork has been doing. Bryan chose to let irreversible actions run
        # unattended, so this is the product reporting back to him rather than
        # instrumentation — and a log that needs an SSH session and a sqlite3
        # prompt to read is one nobody reads.
        "actions": _all(
            conn,
            "SELECT id, created_at, kind, player_name, slot, paired_player_name, reason,"
            " reversible, status, outcome_detail, reported_at"
            " FROM actions ORDER BY id DESC LIMIT 10",
        ),
        "mcp_calls": _all(
            conn,
            "SELECT created_at, tool, outcome, detail FROM mcp_calls ORDER BY id DESC LIMIT 10",
        ),
        "mcp_enabled": bool((settings.mcp_token or "").strip()),
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
                "position_word": position_word(row["position"]),
                "pro_team": row["pro_team"],
                "bye_week": row["bye_week"],
                "injury": (row["injury_status"] or "").upper(),
                "hurt": (row["injury_status"] or "").upper() not in HEALTHY,
            }
        )

    groups = []
    for slot in sorted(set(configured) | set(players_by_slot), key=slot_sort_key):
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
    """The league, and who picks when — from the same order the draft page uses.

    ``teams.draft_slot`` is **not** that order. It is re-seeded from ESPN's
    pre-draft ``pickOrder`` by every sync, and on this league — ``orderType`` is
    ``DRAFT_START`` — that is a placeholder until the draft opens. Rendering it
    under a heading that says "Draft order", with "Your team" beside one row,
    would have this page contradicting the draft page about which pick is hers
    for the whole night.

    So the numbers come from :func:`stored_draft_order`, the order the draft loop
    read off ESPN's own board, and the page says plainly when there is no such
    order yet. A slot is left blank rather than guessed for a team the drawn
    order does not name.
    """
    league = _one(conn, "SELECT * FROM league_settings WHERE id = 1")
    rows = _all(
        conn,
        "SELECT team_id, name, owner, abbrev, draft_slot FROM teams"
        " ORDER BY CASE WHEN draft_slot IS NULL THEN 1 ELSE 0 END, draft_slot, team_id",
    )

    drawn = draft_store.stored_draft_order(conn)
    slots = {team_id: slot for slot, team_id in enumerate(drawn, start=1)}
    teams = [dict(row) for row in rows]
    if drawn:
        for team in teams:
            team["draft_slot"] = slots.get(team["team_id"])
        teams.sort(key=lambda team: (team["draft_slot"] is None, team["draft_slot"] or 0))

    return {
        "league": league,
        "teams": teams,
        "my_team_id": settings.team_id,
        "order_is_final": bool(drawn),
    }


# --- the real-world defaults --------------------------------------------------


def _default_check_auth(settings: Settings) -> Callable[[], tuple[bool, str]]:
    """The live ESPN cookie check, run in a worker thread by the caller."""

    def check() -> tuple[bool, str]:
        from hal_mary.espn import EspnClient

        return EspnClient(settings).check_auth()

    return check


def _default_make_runner(settings: Settings, conn: sqlite3.Connection) -> Any:
    """The real runner, built on the connection its own worker thread opened.

    Imported here rather than at module scope so ``hal-mary --help`` does not
    pay for it, and so the chat page is the only thing that pulls it in.
    """
    from hal_mary.claude_runner import ClaudeRunner

    return ClaudeRunner(settings, conn)


def _default_run_sync(settings: Settings) -> Callable[[], dict[str, Any]]:
    """A real sync, for the button on the status page.

    The connection is opened **inside** this function because it runs on a
    worker thread: a ``sqlite3.Connection`` belongs to the thread that created
    it, so one made on the event loop and used here would raise.
    """

    def run() -> dict[str, Any]:
        from hal_mary.espn import EspnClient, sync_draft, sync_league
        from hal_mary.jobs.lineup_actions import refresh_after_sync

        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
            client = EspnClient(settings)
            summary = dict(sync_league(conn, client))
            summary["picks"] = len(sync_draft(conn, client))
            # The roster and the week have just changed, which is exactly when a
            # bye-week bench becomes true or stops being true. Never raises; see
            # lineup_actions.refresh_after_sync.
            refresh_after_sync(conn, settings)
            return summary
        finally:
            conn.close()

    return run
