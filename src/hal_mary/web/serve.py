"""Running the web app, and telling a human where to find it.

The URL matters more than it looks. The app binds ``0.0.0.0`` so a phone can
reach it, and ``http://0.0.0.0:8080`` is not a URL a phone can open. Printing
the box's actual address is the difference between "it's running" and "she can
use it".
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from typing import Any

__all__ = [
    "app_from_env",
    "lan_url",
    "outbound_ip",
    "run_server",
    "serve",
    "start_draft_loop",
]

log = logging.getLogger(__name__)

#: Import string uvicorn loads. A string plus ``factory=True`` rather than a
#: built app object because ``--reload`` re-imports it in a subprocess, and a
#: single code path is one fewer thing that only breaks in development.
APP_FACTORY = "hal_mary.web.serve:app_from_env"

#: Hosts that mean "every interface", and so tell a phone nothing.
WILDCARD_HOSTS = {"0.0.0.0", "::", "*", ""}

#: Any routable address will do: connecting a UDP socket sends no packets, it
#: just asks the kernel which local address it *would* use to get there. That
#: answer is the LAN address, which is what we want to print.
_PROBE_TARGET = ("192.0.2.1", 9)


def outbound_ip() -> str:
    """The address this box would use to reach the rest of the network.

    ``socket.gethostbyname(gethostname())`` is the usual guess and it is wrong
    on most Linux boxes, where it returns 127.0.1.1 from /etc/hosts.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(_PROBE_TARGET)
        return str(sock.getsockname()[0])
    finally:
        sock.close()


def lan_url(host: str, port: int, resolve: Callable[[], str] | None = None) -> str:
    """The URL to type into a phone.

    An explicitly configured host is kept as-is — someone who bound to one
    interface meant it. A wildcard is resolved to a real address, and falls back
    to localhost rather than failing: a wrong URL beats a server that refused to
    start over a cosmetic detail.
    """
    if host not in WILDCARD_HOSTS:
        return f"http://{host}:{port}"
    try:
        address = (resolve or outbound_ip)()
    except OSError:
        address = "127.0.0.1"
    return f"http://{address}:{port}"


def start_draft_loop(app: Any, settings: Any, thread_factory: Any = None) -> Any:
    """Run the draft loop beside the web app, on its own thread.

    It is handed **the app's own event bus**, which is the whole point: the loop
    marks players gone and writes advice on one thread, and the page finds out
    through ``/events`` on another. ``EventBus.publish`` is built for that
    hand-off.

    A loop that will not start is logged and nothing more. Missing ESPN cookies
    are a real state on draft morning, and the page is where she would find that
    out — serving it is more important than the loop that feeds it.
    """
    from hal_mary.draft.runner import DraftLoopThread

    factory = thread_factory or DraftLoopThread
    thread = factory(settings, app.state.bus)
    app.state.draft_loop = thread
    if thread.start():
        log.info("the draft loop is running beside the web app")
        # The thread is a daemon, so the process exits without this; asking it
        # to finish its tick first is what stops a half-written pick. This
        # FastAPI has no ``add_event_handler``, and ``on_event`` is deprecated,
        # so the router's own list is the stable seam.
        app.router.on_shutdown.append(thread.stop)
    else:
        log.warning(
            "the draft loop is not running (%s); the web app is serving anyway, "
            "and picks can be entered by hand on the draft page",
            thread.error,
        )
    return thread


def app_from_env() -> Any:
    """Build the app from ``config.toml`` and ``.env`` — uvicorn's entry point.

    Migrations run here, once, before anything serves: a request that hits an
    unmigrated database is a 500 on the page you would go to in order to find
    out what is wrong.

    The draft loop starts here too, rather than in ``serve``, because this is
    where the app — and so the event bus the loop publishes onto — is built. It
    is also what ``--reload`` re-runs in its subprocess, so development and
    production take the same path.
    """
    from hal_mary import db
    from hal_mary.config import load_settings
    from hal_mary.web.app import create_app

    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    app = create_app(settings)
    start_draft_loop(app, settings)
    return app


def run_server(*, host: str, port: int, reload: bool) -> None:
    """Hand off to uvicorn. Separated so the CLI is testable without a server."""
    import uvicorn

    uvicorn.run(APP_FACTORY, factory=True, host=host, port=port, reload=reload)


def serve(settings: Any, *, reload: bool = False) -> int:
    """Print where to find it, then run it until interrupted."""
    host, port = settings.web.host, settings.web.port
    url = lan_url(host, port)

    print("hal-mary is starting.")
    print(f"  Open on your phone:  {url}")
    print(f"  Listening on:        {host}:{port}")
    print("  Password:            WEB_PASSWORD in .env")
    missing = [key for key in settings.missing_secrets() if key != "WEB_PASSWORD"]
    if missing:
        print(f"  Not configured yet:  {', '.join(missing)} (see the status page)")
    print("  Stop with Ctrl-C.")

    run_server(host=host, port=port, reload=reload)
    return 0
