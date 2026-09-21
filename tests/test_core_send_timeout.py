"""A wait that ends early must not leave the connection poisoned.

``core.cdp_client.CDPClient`` keeps one ``_pending`` entry per in-flight
command and one receive loop for the whole connection. When a waiter goes
away before Chrome answers - a timeout, a cancellation - the entry it left
behind is still there when the late response arrives. The loop finds it,
delivers to a future nobody holds, and ``InvalidStateError`` reaches its own
``except Exception``, which answers by returning. That kills the receive
loop, and with it every command on that connection.

It matters more than it used to. A page-level connection lived for one
command. RFC-03's Step Run shares one connection across every step, so one
step that times out would take the rest of the run with it.

These tests drive the real ``send`` and the real ``_recv_loop``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from browser_tools.core.cdp_client import CDPClient

_STOP = object()


class ControllableWebSocket:
    """A websocket that answers only what the test decides to answer.

    Async-iterable, so the real ``_recv_loop`` consumes it unchanged.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._inbox.get()
        if item is _STOP:
            raise StopAsyncIteration
        return item

    def deliver(self, message: dict) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def close(self) -> None:
        self._inbox.put_nowait(_STOP)


async def _settle() -> None:
    """Give the receive loop a turn to drain what was just delivered."""
    for _ in range(4):
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def live():
    """A connected client with a real receive loop and a silent socket."""
    cdp = CDPClient("ws://test")
    ws = ControllableWebSocket()
    cdp._ws = ws
    cdp._connected = True
    loop_task = asyncio.create_task(cdp._recv_loop())
    await _settle()
    try:
        yield cdp, ws, loop_task
    finally:
        ws.close()
        await asyncio.wait_for(loop_task, timeout=1)


class TestATimedOutCommandCleansUpAfterItself:
    @pytest.mark.asyncio
    async def test_the_wait_ends_at_the_deadline(self, live):
        cdp, _ws, _loop = live
        with pytest.raises(TimeoutError):
            await cdp.send("Page.enable", timeout=0.05)

    @pytest.mark.asyncio
    async def test_the_pending_entry_is_retired(self, live):
        cdp, _ws, _loop = live
        with pytest.raises(TimeoutError):
            await cdp.send("Page.enable", timeout=0.05)
        assert cdp._pending == {}, "a stale entry outlived its waiter"

    @pytest.mark.asyncio
    async def test_the_late_response_does_not_kill_the_connection(self, live):
        cdp, ws, loop_task = live
        with pytest.raises(TimeoutError):
            await cdp.send("Page.enable", timeout=0.05)

        ws.deliver({"id": 1, "result": {"answered": "too late"}})
        await _settle()

        assert not loop_task.done(), "the receive loop exited on a late response"
        assert cdp._connected is True

    @pytest.mark.asyncio
    async def test_the_next_command_still_works(self, live):
        cdp, ws, _loop = live
        with pytest.raises(TimeoutError):
            await cdp.send("Page.enable", timeout=0.05)
        ws.deliver({"id": 1, "result": {"answered": "too late"}})
        await _settle()

        pending = asyncio.create_task(cdp.send("Page.getFrameTree", timeout=1))
        await _settle()
        ws.deliver({"id": 2, "result": {"frameTree": {"id": "F1"}}})

        assert await pending == {"frameTree": {"id": "F1"}}


class TestACancelledCommandCleansUpAfterItself:
    """The route the timeout does not cover.

    A caller with no deadline can still be cancelled, by its own task group
    or by teardown. The entry has to go the same way.
    """

    @pytest.mark.asyncio
    async def test_the_pending_entry_is_retired(self, live):
        cdp, _ws, _loop = live
        inflight = asyncio.create_task(cdp.send("Page.enable"))
        await _settle()
        assert cdp._pending, "the command never reached the wire"

        inflight.cancel()
        with pytest.raises(asyncio.CancelledError):
            await inflight

        assert cdp._pending == {}

    @pytest.mark.asyncio
    async def test_the_late_response_does_not_kill_the_connection(self, live):
        cdp, ws, loop_task = live
        inflight = asyncio.create_task(cdp.send("Page.enable"))
        await _settle()
        inflight.cancel()
        with pytest.raises(asyncio.CancelledError):
            await inflight

        ws.deliver({"id": 1, "result": {}})
        await _settle()

        assert not loop_task.done()
        assert cdp._connected is True


class TestTheReceiveLoopToleratesADeadFuture:
    """Belt to the retirement's braces.

    The entry can be retired from the waiter's side, but a future can also be
    cancelled where the client cannot see it. The loop drops the message
    instead of raising into its own handler.
    """

    @pytest.mark.asyncio
    async def test_a_response_for_a_cancelled_future_is_dropped(self, live):
        cdp, ws, loop_task = live
        orphan: asyncio.Future = asyncio.get_running_loop().create_future()
        orphan.cancel()
        cdp._pending[99] = orphan

        ws.deliver({"id": 99, "result": {"ignored": True}})
        await _settle()

        assert not loop_task.done(), "the receive loop raised on a dead future"
        assert 99 not in cdp._pending

    @pytest.mark.asyncio
    async def test_an_error_for_a_cancelled_future_is_dropped(self, live):
        cdp, ws, loop_task = live
        orphan: asyncio.Future = asyncio.get_running_loop().create_future()
        orphan.cancel()
        cdp._pending[99] = orphan

        ws.deliver({"id": 99, "error": {"code": -32000, "message": "nope"}})
        await _settle()

        assert not loop_task.done()


class TestNoDeadlineIsStillTheDefault:
    """Existing One-Shot Session callers pass no timeout and must not change."""

    @pytest.mark.asyncio
    async def test_a_command_without_a_timeout_waits(self, live):
        cdp, ws, _loop = live
        pending = asyncio.create_task(cdp.send("Page.enable"))
        await asyncio.sleep(0.1)
        assert not pending.done(), "the send gave up without being asked to"

        ws.deliver({"id": 1, "result": {"ok": True}})
        assert await pending == {"ok": True}

    @pytest.mark.asyncio
    async def test_a_protocol_error_still_raises_the_core_error(self, live):
        from browser_tools.core.errors import CDPError

        cdp, ws, _loop = live
        pending = asyncio.create_task(cdp.send("DOM.querySelector", timeout=1))
        await _settle()
        ws.deliver({"id": 1, "error": {"code": -32000, "message": "No node"}})

        with pytest.raises(CDPError):
            await pending
