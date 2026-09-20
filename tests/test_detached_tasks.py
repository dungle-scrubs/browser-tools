"""A scheduled coroutine must stay owned by whoever scheduled it.

Two sites fired a task and kept nothing: the screencast frame ack and the
``list_frames`` frame-tree refresh. The event loop holds only a weak
reference, so an unheld task can be garbage-collected mid-flight, and an
exception inside it reaches no owner and surfaces later as "Task exception
was never retrieved" (#32).

## The screencast ack

``on_frame`` runs on the CDP read loop, so it cannot await the ack: the read
loop is what resolves the ack's response, and awaiting inline would deadlock.
It schedules the ack instead, and must hold it. A dropped ack stalls the
stream through CDP flow control.

## The list_frames refresh

``_handle_list_frames`` runs on the MCP dispatch path, not the read loop, so
nothing stopped it awaiting. It can just await.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from browser_tools.screencast import ScreencastRecorder


def _connected_cdp(send: Any = None) -> MagicMock:
    """A CDP client mock whose send() the caller can steer."""
    cdp = MagicMock()
    cdp.connected = True
    cdp.send = send or AsyncMock(return_value={})
    return cdp


class TestAckTaskOwnership:
    @pytest.mark.asyncio
    async def test_a_pending_ack_is_held_by_the_recorder(self) -> None:
        """An in-flight ack must have a strong reference, or the GC can drop it."""
        release = asyncio.Event()

        async def slow_send(method: str, params: dict[str, Any] | None = None) -> dict:
            if method == "Page.screencastFrameAck":
                await release.wait()
            return {}

        recorder = ScreencastRecorder()
        await recorder.start(_connected_cdp(send=slow_send), {})

        recorder.on_frame({"data": "QUJD", "sessionId": 7})
        await asyncio.sleep(0)

        assert recorder.pending_acks == 1, "the recorder is not holding the ack task"

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_a_finished_ack_is_released(self) -> None:
        """Completed acks must not accumulate for the life of the capture."""
        recorder = ScreencastRecorder()
        await recorder.start(_connected_cdp(), {})

        for session_id in range(5):
            recorder.on_frame({"data": "QUJD", "sessionId": session_id})
        for _ in range(5):
            await asyncio.sleep(0)

        assert recorder.pending_acks == 0

    @pytest.mark.asyncio
    async def test_stop_drains_in_flight_acks(self, tmp_path) -> None:
        """No ack task may outlive the capture that scheduled it."""
        release = asyncio.Event()

        async def slow_send(method: str, params: dict[str, Any] | None = None) -> dict:
            if method == "Page.screencastFrameAck":
                await release.wait()
            return {}

        cdp = _connected_cdp(send=slow_send)
        recorder = ScreencastRecorder()
        await recorder.start(cdp, {})
        recorder.on_frame({"data": "QUJD", "sessionId": 7})
        await asyncio.sleep(0)
        assert recorder.pending_acks == 1

        await recorder.stop(cdp, {"dir": str(tmp_path)})

        assert recorder.pending_acks == 0, "stop left an ack task running"

    @pytest.mark.asyncio
    async def test_an_ack_failure_does_not_escape_the_recorder(self) -> None:
        """A failing ack must be handled, not left for the loop to complain about."""
        unhandled: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unhandled.append(context)
        )

        async def failing_send(method: str, params: dict[str, Any] | None = None) -> dict:
            if method == "Page.screencastFrameAck":
                raise RuntimeError("ack refused")
            return {}

        recorder = ScreencastRecorder()
        await recorder.start(_connected_cdp(send=failing_send), {})

        recorder.on_frame({"data": "QUJD", "sessionId": 7})
        for _ in range(5):
            await asyncio.sleep(0)

        assert recorder.pending_acks == 0
        assert unhandled == [], f"ack failure reached the loop: {unhandled}"


class TestListFramesAwaitsItsRefresh:
    """``list_frames`` fired a refresh it never waited for.

    The refresh ran as a detached task, so the tool answered "Refreshing
    frame tree..." and the caller had to ask again for the frames the
    refresh had already fetched. Nothing forces that split: the handler runs
    on the MCP dispatch path, not the CDP read loop, so it can await.
    """

    @staticmethod
    def _handler_with_empty_frame_tree(frames_after_refresh: list[dict[str, Any]]):
        """A handler whose frame manager fills in only once a refresh runs."""
        from browser_tools.cdp_handler import CDPHandler as BrowserCDPHandler
        from browser_tools.cdp_handler import CDPRuntime

        known: list[dict[str, Any]] = []

        cdp = MagicMock()
        cdp.connected = True

        async def send(method: str, params: dict[str, Any] | None = None) -> dict:
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "F1"}}}
            return {}

        cdp.send = send

        fm = MagicMock()
        fm.get_flat_frames.side_effect = lambda *a, **k: list(known)
        fm.selected_frame_id = None
        fm.update_from_frame_tree.side_effect = lambda _tree: known.extend(
            frames_after_refresh
        )

        handler = BrowserCDPHandler.__new__(BrowserCDPHandler)
        rt = CDPRuntime.__new__(CDPRuntime)
        rt._cdp_client = cdp
        rt._frame_manager = fm
        handler._rt = rt
        return handler

    @pytest.mark.asyncio
    async def test_the_refreshed_frames_are_in_the_reply(self) -> None:
        """One call must return the frames the refresh found."""
        handler = self._handler_with_empty_frame_tree(
            [{"frameId": "F1", "url": "https://example.com/", "depth": 0, "name": ""}]
        )

        result = await handler._handle_list_frames({})

        text = result["result"]["content"][0]["text"]
        assert "F1" in text, f"list_frames did not report the refreshed frame: {text!r}"
        assert "Refreshing" not in text

    @pytest.mark.asyncio
    async def test_a_refresh_that_finds_nothing_says_so(self) -> None:
        """An empty page after a refresh reports no frames, not a pending refresh."""
        handler = self._handler_with_empty_frame_tree([])

        result = await handler._handle_list_frames({})

        text = result["result"]["content"][0]["text"]
        assert "No frames available" in text
        assert "Refreshing" not in text
