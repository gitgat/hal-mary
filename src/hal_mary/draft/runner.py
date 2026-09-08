"""Running the draft loop beside the web app, on its own thread.

``hal-mary serve`` starts one process. The web app owns an asyncio event loop
and the draft loop wants one too, and they cannot share either that loop or a
database connection:

* :meth:`DraftLoop.run_once` does blocking SQLite and subprocess work on
  whatever loop calls it. On the web app's loop that would freeze every request
  for the length of a Claude call — including ``/events``, which is how the page
  finds out anything happened.
* ``db.connect`` leaves ``check_same_thread`` on, so a connection made on the
  web thread and used here is an intermittent ``ProgrammingError`` rather than a
  clean failure. The thread opens its own.

The two halves talk through the :class:`~hal_mary.events.EventBus`, which is
built for exactly this hand-off: ``publish`` is safe from any thread and wakes
the web app's subscribers on their own loop.

**A loop failure must never take the web app down.** Missing ESPN cookies, an
unmigrated database, a ``claude`` binary that is not installed — every one of
those is a real state on draft morning, and in every one of them the page still
has to render, because the page is where she would find out. So ``start``
returns a bool and records why rather than raising, and the thread's own body
swallows whatever escapes ``run_forever``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

from hal_mary import db
from hal_mary.config import Settings

__all__ = ["DraftLoopThread", "build_draft_loop"]

log = logging.getLogger(__name__)

#: How long ``stop`` waits for the thread to finish its tick before giving up on
#: it. Not a tunable of behaviour: it only bounds shutdown, and a tick that has
#: not ended by then is a process that is exiting anyway.
JOIN_TIMEOUT_S = 10.0

#: How long ``start`` waits to hear whether the loop got off the ground. The
#: work it covers is opening a local SQLite file, running migrations and
#: constructing two objects — none of which touches the network — so this is
#: generous rather than tuned, and timing out only means ``start`` reports "not
#: yet" for a loop that may still come up.
START_TIMEOUT_S = 10.0


def build_draft_loop(settings: Settings, conn: sqlite3.Connection, bus: Any) -> Any:
    """The real loop, with the real ESPN client and the real Claude runner.

    Imported lazily so that a box with no ESPN credentials still starts the web
    app: everything expensive or credential-dependent is constructed here, on
    the loop's own thread, and a failure is caught by the caller.
    """
    from hal_mary.claude_runner import ClaudeRunner
    from hal_mary.draft.loop import DraftLoop
    from hal_mary.espn import EspnClient

    return DraftLoop(conn, settings, EspnClient(settings), ClaudeRunner(settings, conn), bus)


class DraftLoopThread:
    """The draft loop, running beside the web app and never taking it with it.

    ``build_loop`` is the seam: it is handed ``(conn, bus)`` and returns anything
    with ``run_forever()`` and ``stop()``. The default builds the real thing.
    """

    def __init__(
        self,
        settings: Settings,
        bus: Any,
        build_loop: Callable[[sqlite3.Connection, Any], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self._build = build_loop or (
            lambda conn, bus: build_draft_loop(settings, conn, bus)
        )
        self._thread: threading.Thread | None = None
        self._loop: Any = None
        self._conn: sqlite3.Connection | None = None
        self._ready = threading.Event()
        self.error: str | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- what the page asks the loop --------------------------------------

    def watching(self) -> dict[str, Any] | None:
        """The loop's own cadence, or ``None`` when there is no loop.

        Read from the web app's thread. Nothing here mutates anything and the
        values are a string and an int, so the read is safe without a lock — and
        the alternative, publishing the cadence into the database on every phase
        change, would put a write on the pick-clock path to say something the
        loop already knows.
        """
        return self._ask("watching")

    def draft_started(self) -> dict[str, Any] | None:
        """"The draft has started", forwarded to the loop. ``None`` if none runs."""
        return self._ask("draft_started")

    def _ask(self, name: str) -> dict[str, Any] | None:
        loop = self._loop
        if loop is None or not self.alive:
            return None
        method = getattr(loop, name, None)
        if method is None:  # pragma: no cover - every real loop has both
            return None
        try:
            return method()
        except Exception:
            log.exception("the draft loop would not answer %s()", name)
            return None

    def start(self, timeout: float = START_TIMEOUT_S) -> bool:
        """Start the thread and report whether the loop got off the ground.

        Never raises. ``False`` leaves :attr:`error` holding the reason, and the
        web app carries on either way — a box with no ESPN cookies still has to
        serve the page that says so.

        The connection and the loop are both built **on the new thread**, not
        here: ``db.connect`` leaves ``check_same_thread`` on, so a connection
        opened on the caller's thread and used by the loop is a
        ``ProgrammingError`` on the first tick. That is why ``start`` waits for
        the thread to say how it went rather than simply returning.
        """
        if self.alive:
            return True
        self.error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hal-mary-draft-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return self.error is None and self.alive

    def _run(self) -> None:
        """The whole life of the loop, on its own thread.

        Everything that can fail is inside this one try: opening the database,
        building the client and the runner, and the loop itself. A failure in
        any of them is recorded and logged, the thread ends, and the process —
        which is serving the web app — does not notice.
        """
        conn: sqlite3.Connection | None = None
        try:
            conn = db.connect(self.settings.db_path)
            db.migrate(conn)
            self._conn = conn
            self._loop = self._build(conn, self.bus)
        except Exception as exc:  # noqa: BLE001 - a broken box is a message
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("the draft loop could not start (%s); the web app carries on", self.error)
            self._close(conn)
            self._ready.set()
            return

        # Only now is there something to stop, so only now is start() told it
        # succeeded.
        self._ready.set()
        try:
            asyncio.run(self._loop.run_forever())
        except Exception as exc:  # the loop dying is not the app dying
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("the draft loop stopped with an error; the web app carries on")
        finally:
            # Closed on the thread that opened it, which is the only thread
            # allowed to touch it.
            self._close(conn)

    def _close(self, conn: sqlite3.Connection | None) -> None:
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # pragma: no cover - closing a connection twice
            log.debug("the draft loop's connection would not close", exc_info=True)

    def stop(self, timeout: float = JOIN_TIMEOUT_S) -> None:
        """Ask the loop to finish its tick, then wait for the thread.

        Wired to the app's shutdown, so this is what a ``systemd`` restart runs:
        ``SIGTERM`` -> uvicorn's lifespan shutdown -> here. It has to be both
        **effective** and **bounded**. Effective because the deploy unit sends
        that signal on every release and a loop that survived it would leave a
        thread polling ESPN behind, one more on each deploy, all of them
        invisible and each one behaving perfectly. Bounded because the process
        is going away regardless: the thread is a daemon, so a tick that will
        not end is logged and abandoned rather than holding the shutdown open.

        ``DraftLoop.stop`` is thread-safe and wakes the loop out of its wait, so
        the join normally returns in milliseconds even on the five-minute idle
        cadence — a wait that had to time out first would put an idle interval
        between every ``systemctl restart`` and the process actually going.
        """
        loop = self._loop
        if loop is not None:
            try:
                loop.stop()
            except Exception:  # stopping must not raise on the way out
                log.debug("the draft loop would not stop cleanly", exc_info=True)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Worth a line: the process still exits (daemon thread), but a
                # tick that outlives its shutdown is how a Claude call or an
                # ESPN read gets cut off mid-write, and this is the only record
                # that it happened.
                log.warning(
                    "the draft loop did not stop within %ss and is still running; the "
                    "process is exiting anyway",
                    timeout,
                )
            else:
                self._thread = None
