"""Tests for the in-process event bus.

The interesting property is not the queue plumbing, it is the thread boundary:
``publish`` is called from the synchronous draft loop while subscribers live on
the web app's event loop. The draft loop must never block, never raise, and
never care whether a browser is open.
"""

import asyncio
import threading

import pytest

from hal_mary.events import EventBus


async def test_publishing_with_no_subscribers_is_a_no_op():
    """The draft loop runs whether or not a phone is looking at it."""
    bus = EventBus()
    assert bus.publish("board_updated", {"pick": 12}) is None
    assert bus.subscriber_count == 0


async def test_a_subscriber_receives_what_is_published():
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish("board_updated", {"pick": 12})
        assert await anext(sub) == ("board_updated", {"pick": 12})


async def test_two_subscribers_each_receive_every_event():
    bus = EventBus()
    async with bus.subscribe() as first, bus.subscribe() as second:
        assert bus.subscriber_count == 2
        bus.publish("advice", {"pick": "a WR"})

        assert await anext(first) == ("advice", {"pick": "a WR"})
        assert await anext(second) == ("advice", {"pick": "a WR"})


async def test_a_full_queue_drops_the_oldest_event_and_keeps_the_newest():
    """A phone that fell asleep mid-draft must not stall the loop tracking picks.
    The newest events are the ones worth having, so the oldest go over the side."""
    bus = EventBus()
    async with bus.subscribe(maxsize=2) as sub:
        for pick in (1, 2, 3):
            bus.publish("board_updated", {"pick": pick})

        assert await anext(sub) == ("board_updated", {"pick": 2})
        assert await anext(sub) == ("board_updated", {"pick": 3})
        assert sub.dropped == 1


async def test_one_slow_subscriber_does_not_cost_a_fast_one_anything():
    bus = EventBus()
    async with bus.subscribe(maxsize=1) as slow, bus.subscribe(maxsize=10) as fast:
        for pick in range(5):
            bus.publish("board_updated", {"pick": pick})

        received = [await anext(fast) for _ in range(5)]
        assert [payload["pick"] for _, payload in received] == [0, 1, 2, 3, 4]
        assert await anext(slow) == ("board_updated", {"pick": 4})
        assert slow.dropped == 4


async def test_a_subscriber_that_exits_is_removed():
    bus = EventBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0

    # And the loop keeps going without it.
    assert bus.publish("board_updated", {"pick": 13}) is None


async def test_a_subscriber_that_exits_by_breaking_out_of_the_loop_is_removed():
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish("board_updated", {"pick": 1})
        async for _event, _payload in sub:
            break
    assert bus.subscriber_count == 0


async def test_closing_a_subscription_ends_its_iteration():
    bus = EventBus()
    sub = bus.subscribe()
    consumed = []

    async def consume():
        async for event, payload in sub:
            consumed.append((event, payload))

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    bus.publish("board_updated", {"pick": 1})
    sub.close()

    await asyncio.wait_for(task, timeout=1)
    assert consumed == [("board_updated", {"pick": 1})]
    assert bus.subscriber_count == 0


async def test_closing_twice_is_harmless():
    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    sub.close()
    assert bus.subscriber_count == 0


async def test_publishing_from_a_thread_reaches_a_subscriber_on_the_loop():
    """This is the real shape of the system: the draft loop is synchronous and
    off the event loop, the web layer is on it."""
    bus = EventBus()
    async with bus.subscribe() as sub:
        thread = threading.Thread(target=bus.publish, args=("board_updated", {"pick": 7}))
        thread.start()
        thread.join()

        got = await asyncio.wait_for(anext(sub), timeout=2)
        assert got == ("board_updated", {"pick": 7})


async def test_publishing_from_a_thread_never_blocks_on_a_full_queue():
    bus = EventBus()
    async with bus.subscribe(maxsize=1) as sub:

        def publish_many():
            for pick in range(50):
                bus.publish("board_updated", {"pick": pick})

        thread = threading.Thread(target=publish_many)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), "publish blocked on a subscriber that was not reading"

        assert await asyncio.wait_for(anext(sub), timeout=2) == ("board_updated", {"pick": 49})


def test_subscribing_without_a_running_loop_is_a_clear_error():
    bus = EventBus()
    # match= matters: NotImplementedError is itself a RuntimeError, so a bare
    # pytest.raises(RuntimeError) here would pass against an unwritten stub.
    with pytest.raises(RuntimeError, match="running event loop"):
        bus.subscribe()


def test_publishing_survives_a_subscriber_whose_loop_has_gone_away():
    """A web worker that died still holds a queue. The draft loop must not."""
    bus = EventBus()

    async def subscribe_and_abandon():
        bus.subscribe()  # deliberately never closed

    asyncio.run(subscribe_and_abandon())
    assert bus.subscriber_count == 1

    assert bus.publish("board_updated", {"pick": 1}) is None
    assert bus.subscriber_count == 0
