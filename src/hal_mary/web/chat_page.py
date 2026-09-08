"""The chat page's half of the web app: its context, and its event stream.

Two things live here that do not fit in ``app.py``.

**Driving a blocking generator from the event loop.** ``ClaudeRunner.stream`` is
blocking, it ends by writing a ``claude_calls`` row, and ``db.connect`` leaves
``check_same_thread`` on — so the thread that consumes the last chunk has to be
the thread that opened the connection. ``iterate_in_threadpool`` does not
satisfy that: it hops threads per ``next()``, and the final chunk then raises
``sqlite3.ProgrammingError`` on a worker that did not create the connection —
intermittently, because anyio usually reuses the same worker, which is the worst
way for it to fail. So one ``run_in_threadpool`` call opens the connection,
builds the runner, drains the whole answer and hands chunks across a queue.

**One answer per conversation at a time.** A phone that locks its screen drops
the stream; the page that comes back finds the question still unanswered and
would ask again, while the first call is still running and still being paid for.
:class:`Answering` is the interlock: a second stream for a conversation that is
already being answered says so and ends, rather than starting a second call that
writes a second reply. One instance per application rather than one per process —
the set is about *this* app's live calls, and a module-level one would have two
apps in one process (which is every test run) refusing each other's streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

from starlette.concurrency import run_in_threadpool

from hal_mary import chat
from hal_mary.config import Settings

__all__ = ["Answering", "chat_context", "chat_event_stream"]

log = logging.getLogger(__name__)


class Answering:
    """The conversations this application has a Claude call in flight for.

    In memory and never persisted: a restart that forgets a call it can no
    longer reach is the right answer, and a stale row would refuse a
    conversation forever.
    """

    def __init__(self) -> None:
        self._live: set[int] = set()
        self._lock = threading.Lock()

    def claim(self, session_id: int) -> bool:
        """Take the conversation, or report that somebody else already has."""
        with self._lock:
            if session_id in self._live:
                return False
            self._live.add(session_id)
            return True

    def release(self, session_id: int) -> None:
        with self._lock:
            self._live.discard(session_id)


#: What the reader gets back when the heartbeat fired before a chunk arrived.
_IDLE = object()

BUSY_MESSAGE = (
    "I am still writing the last answer. Give it a moment and reload the page."
)


def chat_context(
    conn: sqlite3.Connection, settings: Settings, session_id: int | None = None
) -> dict[str, Any]:
    """Everything the chat page shows. Never raises on a database in any state."""
    sessions = chat.list_sessions(conn, limit=settings.chat.session_limit)

    current = chat.get_session(conn, session_id) if session_id is not None else None
    if current is None:
        current = sessions[0] if sessions else None

    here = int(current["id"]) if current is not None else None
    return {
        "sessions": sessions,
        "session": current,
        "session_id": here,
        "messages": chat.get_messages(conn, here) if here is not None else [],
        # The page draws an empty bubble for a question nothing has answered and
        # points it at the stream. That covers both halves of the round trip: the
        # fragment HTMX swaps in, and a page reloaded before the answer landed.
        "pending": here is not None and chat.pending_question(conn, here) is not None,
    }


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _take(out: queue.Queue, timeout: float) -> Any:
    try:
        return out.get(timeout=timeout)
    except queue.Empty:
        return _IDLE


async def chat_event_stream(
    open_conn: Callable[[], sqlite3.Connection],
    settings: Settings,
    make_runner: Callable[[Settings, sqlite3.Connection], Any],
    answering: Answering,
    session_id: int,
    heartbeat_s: float,
) -> AsyncIterator[str]:
    """Answer the conversation's outstanding question, a token at a time.

    Frames: ``chunk`` for text as it arrives, ``busy`` when another stream is
    already answering, and exactly one ``done`` at the end. Comment frames keep
    a phone's connection open; without them a browser or anything proxying in
    between closes a stream that has said nothing for a minute, which on a call
    that is searching the web is most of them.

    A stream with nothing outstanding ends immediately. That is what a reload
    after the answer landed does, and it must not cost another Claude call.
    """
    out: queue.Queue = queue.Queue()
    stop = threading.Event()

    def consume() -> None:
        """The whole call, on one thread, owning one connection.

        Everything is inside the ``try``, opening the connection included. The
        reader ends on the sentinel this puts back, so a worker that fell over
        before reaching its own ``finally`` would leave the phone on an endless
        heartbeat with nothing on the other end of it.
        """
        conn: sqlite3.Connection | None = None
        try:
            conn = open_conn()
            question = chat.pending_question(conn, session_id)
            if question is None:
                return
            if not answering.claim(session_id):
                out.put(_frame("busy", {"message": BUSY_MESSAGE}))
                return
            try:
                stream = chat.answer(
                    conn,
                    settings,
                    make_runner(settings, conn),
                    session_id,
                    question["content"],
                )
                try:
                    for chunk in stream:
                        if stop.is_set():
                            break
                        out.put(chunk)
                finally:
                    # Closing it is what persists a partial reply: the browser
                    # went away, and what arrived is worth more than nothing.
                    stream.close()
            finally:
                answering.release(session_id)
        except Exception:  # the reader must still be told it is over
            log.exception("the chat stream for session %s failed", session_id)
        finally:
            out.put(None)
            if conn is not None:
                conn.close()

    task = asyncio.ensure_future(run_in_threadpool(consume))
    task.add_done_callback(_log_failure)

    try:
        # Immediately, so the response headers flush and the page knows it is
        # connected before anything waits on a model.
        yield ": connected\n\n"
        while True:
            item = await run_in_threadpool(_take, out, heartbeat_s)
            if item is _IDLE:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            if isinstance(item, str):
                yield item
            elif item.kind == "text" and item.text:
                yield _frame("chunk", {"text": item.text})
        yield _frame("done", {})
    finally:
        # The phone went away. The worker checks this between chunks; it cannot
        # interrupt a read already blocked on the subprocess, so the call it has
        # paid for finishes and persists rather than being thrown away.
        stop.set()


def _log_failure(task: asyncio.Future) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover - consume() catches its own
        log.error("the chat stream worker died: %r", exc)
