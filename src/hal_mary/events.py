"""A small in-process publish/subscribe hub.

The draft loop publishes; the web layer's Server-Sent Events endpoint
subscribes. Both live in one process, so this is a hand-off between a thread and
an event loop, not a message broker.

The whole design turns on one asymmetry:

    ``publish()`` is synchronous and can be called from any thread. Subscribers
    live on an asyncio event loop and are woken through ``asyncio.Queue``, which
    is **not** thread-safe.

Touching another thread's ``asyncio.Queue`` directly is the classic version of
this bug: it appears to work, because ``put_nowait`` on a queue with room is
just a ``deque.append``, and then it loses a wakeup under load and an event
never arrives -- silently, mid-draft.

So a subscription records the loop it was created on
(``asyncio.get_running_loop()`` at ``subscribe()`` time), and ``publish`` never
touches a queue itself. It hands the delivery to that loop with
``loop.call_soon_threadsafe``, the one asyncio API documented as safe to call
from another thread: it appends to the loop's callback queue under a lock and
writes to the loop's self-pipe to wake it. Every mutation of every queue
therefore happens on the loop that owns it, single-threaded, and the publisher
returns immediately without waiting for any of it.

When ``publish`` is already running on a subscriber's own loop -- the web layer
publishing to itself -- delivery is inline instead, which keeps ordering
obvious and saves a scheduling hop. It is the same call, just not deferred.

Two consequences the draft loop depends on:

* Publishing with no subscribers is a no-op, never an error. The loop runs
  whether or not a browser is open.
* A slow subscriber cannot apply backpressure. Each queue is bounded, and a full
  queue drops its **oldest** event to make room for the newest. A phone that
  slept through six picks wants the current board, not a backlog, and the loop
  that is tracking picks must never wait for it.
"""

from __future__ import annotations

import asyncio
import threading
from types import TracebackType
from typing import Any, Self

__all__ = ["DEFAULT_MAX_QUEUE", "EventBus", "Subscription"]

#: How many undelivered events a subscriber may hold before the oldest are
#: dropped. Not in config.toml: it is a memory bound on a data structure, not a
#: tunable of behaviour, and a caller that cares passes ``maxsize=`` instead.
#: Large enough that a healthy browser never drops, small enough that a hundred
#: dead subscriptions cost nothing that matters.
DEFAULT_MAX_QUEUE = 256

#: Sentinel pushed into a queue to end its iteration on close.
_CLOSED = object()


class Subscription:
    """One subscriber's view of the bus: an async iterator of
    ``(event, payload)``.

    Created by :meth:`EventBus.subscribe`, never directly. Registered with the
    bus from the moment it is created, so nothing published between
    ``subscribe()`` and the first ``__anext__`` is missed.

    Use it as an async context manager, which unsubscribes on the way out even
    if the client disconnected mid-iteration::

        async with bus.subscribe() as events:
            async for event, payload in events:
                ...

    Iteration ends when :meth:`close` is called. ``close`` and iteration both
    belong to the loop the subscription was created on.
    """

    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self._bus = bus
        self._loop = loop
        self._queue: asyncio.Queue[tuple[str, dict] | object] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """How many events were discarded because this subscriber was behind."""
        return self._dropped

    def close(self) -> None:
        """Unsubscribe. Idempotent, and safe to call while iterating: a pending
        ``__anext__`` raises ``StopAsyncIteration`` rather than hanging."""
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        self._offer(_CLOSED)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> tuple[str, dict]:
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item  # type: ignore[return-value]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- called only on self._loop -----------------------------------------

    def _deliver(self, event: str, payload: dict) -> None:
        if self._closed:
            return
        self._offer((event, payload))

    def _offer(self, item: Any) -> None:
        """Enqueue, dropping the oldest event if the queue is full.

        Runs on the owning loop, so the get/put pair below cannot interleave
        with another producer and the queue is never left empty with a waiter
        parked on it.
        """
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - needs maxsize 0
                    return
                self._dropped += 1


class EventBus:
    """Fan-out of ``(event, payload)`` to every live subscriber.

    ``publish`` is safe from any thread. ``subscribe`` must be called from a
    coroutine, because a subscription is bound to the loop that will read it.
    """

    def __init__(self) -> None:
        # A list keeps fan-out order deterministic. The lock is held only for
        # the three O(n) list operations below, never across a delivery, and
        # never while waiting on anything -- publish adds and removes from a
        # foreign thread while the loop thread subscribes and closes.
        self._lock = threading.Lock()
        self._subscriptions: list[Subscription] = []

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def subscribe(self, *, maxsize: int = DEFAULT_MAX_QUEUE) -> Subscription:
        """Register a subscriber and return its :class:`Subscription`.

        ``maxsize`` bounds how far behind this subscriber may fall before its
        oldest events are dropped.

        Raises ``RuntimeError`` when called outside a running event loop: the
        subscription has to know which loop to be woken on, and guessing is how
        events get delivered to a loop that will never run them.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "EventBus.subscribe() must be called from a running event loop; "
                "a subscription is bound to the loop that reads it"
            ) from exc
        if maxsize < 1:
            raise ValueError(f"maxsize must be at least 1, got {maxsize}")

        subscription = Subscription(self, loop, maxsize)
        with self._lock:
            self._subscriptions.append(subscription)
        return subscription

    def publish(self, event: str, payload: dict) -> None:
        """Deliver ``(event, payload)`` to every subscriber and return at once.

        Callable from any thread, including one with no event loop of its own,
        which is how the synchronous draft loop uses it. Never blocks, never
        raises for the caller's benefit: no subscribers is a no-op, a subscriber
        whose loop has already shut down is quietly dropped, and a subscriber
        that is not keeping up loses its oldest events rather than slowing this
        call down.

        ``payload`` is handed to every subscriber by reference. Treat it as
        immutable once published -- build a new dict per publish rather than
        mutating one the subscribers may still be reading.
        """
        with self._lock:
            subscriptions = list(self._subscriptions)
        if not subscriptions:
            return

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None

        for subscription in subscriptions:
            if subscription._loop is current:
                # Already on the loop that owns this queue: deliver inline.
                subscription._deliver(event, payload)
                continue
            try:
                subscription._loop.call_soon_threadsafe(subscription._deliver, event, payload)
            except RuntimeError:
                # The loop is closed or closing; the subscriber is gone and no
                # amount of trying will reach it. The draft loop does not care.
                self._remove(subscription)

    def _remove(self, subscription: Subscription) -> None:
        with self._lock:
            try:
                self._subscriptions.remove(subscription)
            except ValueError:
                pass
