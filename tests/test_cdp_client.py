"""Tests for the CDP WebSocket client (M-2.1)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from browser_tools.cdp_client import CDPClient, CDPError, get_browser_ws_url, get_page_ws_url


class FakeWebSocket:
    """Fake WebSocket for testing CDPClient without a real Chrome instance."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._messages: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False

    async def send(self, data: str) -> None:
        """Record sent messages."""
        self.sent.append(data)

    async def close(self) -> None:
        """Mark connection as closed."""
        self._closed = True
        # Push a sentinel to break the async iterator
        await self._messages.put("")

    def inject_message(self, message: dict[str, Any]) -> None:
        """Queue a message to be received by the read loop."""
        self._messages.put_nowait(json.dumps(message))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        msg = await self._messages.get()
        if self._closed and msg == "":
            raise StopAsyncIteration
        if not msg:
            raise StopAsyncIteration
        return msg


@pytest.fixture
def fake_ws():
    """Create a FakeWebSocket."""
    return FakeWebSocket()


@pytest.fixture
def cdp_client(fake_ws):
    """Create a CDPClient with a fake WebSocket (pre-connected)."""
    client = CDPClient("ws://fake:9222/devtools/browser/test", timeout=5.0)
    client._ws = fake_ws
    client._connected = True
    client._reader_task = asyncio.ensure_future(_noop_coro())
    return client


async def _noop_coro():
    pass


class TestCDPClientConstruction:
    """Tests for CDPClient initialization."""

    def test_initial_state(self) -> None:
        """New client should be disconnected with no pending messages."""
        client = CDPClient("ws://test:9222")
        assert client.connected is False
        assert client._msg_id == 0
        assert client._pending == {}

    def test_stores_ws_url(self) -> None:
        """Client should store the WebSocket URL."""
        client = CDPClient("ws://test:9222/devtools/browser/abc")
        assert client._ws_url == "ws://test:9222/devtools/browser/abc"


class AutoRespondWebSocket(FakeWebSocket):
    """FakeWebSocket that auto-responds to CDP commands."""

    def __init__(
        self, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self._auto_result = result or {}
        self._auto_error = error

    async def send(self, data: str) -> None:
        """Record sent message and auto-inject a response."""
        self.sent.append(data)
        msg = json.loads(data)
        msg_id = msg.get("id")
        if msg_id is not None:
            if self._auto_error:
                self.inject_message({"id": msg_id, "error": self._auto_error})
            else:
                self.inject_message({"id": msg_id, "result": self._auto_result})


class TestCDPClientSend:
    """Tests for sending CDP commands."""

    @pytest.mark.asyncio
    async def test_send_increments_msg_id(self) -> None:
        """Each send should use a unique incrementing message ID."""
        ws = AutoRespondWebSocket()
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        await client.send("Page.enable")
        assert client._msg_id >= 1
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_send_formats_message_correctly(self) -> None:
        """Send should format proper JSON-RPC style CDP messages."""
        ws = AutoRespondWebSocket()
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        await client.send("Runtime.evaluate", {"expression": "1+1"})
        sent = json.loads(ws.sent[0])
        assert sent["method"] == "Runtime.evaluate"
        assert sent["params"] == {"expression": "1+1"}
        assert "id" in sent
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_send_raises_when_disconnected(self) -> None:
        """Sending on a disconnected client should raise CDPError."""
        client = CDPClient("ws://fake:9222")
        with pytest.raises(CDPError, match="Not connected"):
            await client.send("Page.enable")

    @pytest.mark.asyncio
    async def test_send_timeout_raises(self, fake_ws: FakeWebSocket) -> None:
        """A timed-out command should raise CDPError."""
        client = CDPClient("ws://fake:9222", timeout=0.1)
        client._ws = fake_ws
        client._connected = True
        # No reader task = no responses, so it will timeout
        client._reader_task = asyncio.create_task(_noop_coro())

        with pytest.raises(CDPError, match="timed out"):
            await client.send("Page.enable", timeout=0.1)

    @pytest.mark.asyncio
    async def test_send_returns_error(self) -> None:
        """CDP error responses should raise CDPError."""
        ws = AutoRespondWebSocket(error={"code": -32000, "message": "Not found"})
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        with pytest.raises(CDPError, match="Not found"):
            await client.send("Page.navigate")
        await client.disconnect()


class TestCDPClientEvents:
    """Tests for CDP event handling."""

    @pytest.mark.asyncio
    async def test_event_handler_called(self, fake_ws: FakeWebSocket) -> None:
        """Registered event handlers should be called for matching events."""
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = fake_ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        received_events: list[dict] = []
        client.on("Page.frameNavigated", lambda params: received_events.append(params))

        fake_ws.inject_message(
            {
                "method": "Page.frameNavigated",
                "params": {"frame": {"id": "F1", "url": "https://example.com"}},
            }
        )
        await asyncio.sleep(0.1)

        assert len(received_events) == 1
        assert received_events[0]["frame"]["id"] == "F1"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_multiple_handlers_for_same_event(self, fake_ws: FakeWebSocket) -> None:
        """Multiple handlers for the same event should all be called."""
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = fake_ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        calls_a: list[dict] = []
        calls_b: list[dict] = []
        client.on("Page.frameAttached", lambda p: calls_a.append(p))
        client.on("Page.frameAttached", lambda p: calls_b.append(p))

        fake_ws.inject_message(
            {
                "method": "Page.frameAttached",
                "params": {"frameId": "F2", "parentFrameId": "F1"},
            }
        )
        await asyncio.sleep(0.1)

        assert len(calls_a) == 1
        assert len(calls_b) == 1
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_off_removes_handler(self, fake_ws: FakeWebSocket) -> None:
        """off() should remove a previously registered handler."""
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = fake_ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        calls: list[dict] = []
        handler = lambda p: calls.append(p)
        client.on("Page.frameDetached", handler)
        client.off("Page.frameDetached", handler)

        fake_ws.inject_message(
            {
                "method": "Page.frameDetached",
                "params": {"frameId": "F1"},
            }
        )
        await asyncio.sleep(0.1)

        assert len(calls) == 0
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_unhandled_events_are_ignored(self, fake_ws: FakeWebSocket) -> None:
        """Events without handlers should not cause errors."""
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = fake_ws
        client._connected = True
        client._reader_task = asyncio.create_task(client._read_loop())

        fake_ws.inject_message(
            {
                "method": "Network.requestWillBeSent",
                "params": {"requestId": "1"},
            }
        )
        await asyncio.sleep(0.1)
        # No crash = success
        await client.disconnect()


class TestCDPClientDisconnect:
    """Tests for graceful disconnection."""

    @pytest.mark.asyncio
    async def test_disconnect_closes_websocket(self, fake_ws: FakeWebSocket) -> None:
        """disconnect() should close the WebSocket."""
        client = CDPClient("ws://fake:9222", timeout=2.0)
        client._ws = fake_ws
        client._connected = True
        client._reader_task = asyncio.create_task(_noop_coro())

        await client.disconnect()
        assert client.connected is False
        assert fake_ws._closed is True

    @pytest.mark.asyncio
    async def test_disconnect_rejects_pending(self, fake_ws: FakeWebSocket) -> None:
        """Pending requests should be rejected on disconnect."""
        client = CDPClient("ws://fake:9222", timeout=10.0)
        client._ws = fake_ws
        client._connected = True
        client._reader_task = asyncio.create_task(_noop_coro())

        # Create a pending future
        future = asyncio.get_event_loop().create_future()
        client._pending[999] = future

        await client.disconnect()
        assert future.done()
        with pytest.raises(CDPError, match="Connection closed"):
            future.result()


class TestURLHelpers:
    """Tests for WebSocket URL resolution helpers."""

    def test_get_browser_ws_url_with_mock(self) -> None:
        """get_browser_ws_url should parse /json/version response."""
        response_data = json.dumps(
            {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}
        ).encode()

        with patch("cdp_client.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = response_data
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = lambda s, *a: None
            mock_urlopen.return_value = mock_response

            url = get_browser_ws_url("http://127.0.0.1:9222")
            assert url == "ws://127.0.0.1:9222/devtools/browser/abc"

    def test_get_browser_ws_url_unreachable(self) -> None:
        """Unreachable endpoint should return None."""
        url = get_browser_ws_url("http://127.0.0.1:1")
        assert url is None

    def test_get_page_ws_url_with_mock(self) -> None:
        """get_page_ws_url should return the first page's WS URL."""
        response_data = json.dumps(
            [
                {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/ABC"},
                {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/DEF"},
            ]
        ).encode()

        with patch("cdp_client.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = response_data
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = lambda s, *a: None
            mock_urlopen.return_value = mock_response

            url = get_page_ws_url("http://127.0.0.1:9222")
            assert url == "ws://127.0.0.1:9222/devtools/page/ABC"

    def test_get_page_ws_url_by_index(self) -> None:
        """get_page_ws_url with index should return the specified page."""
        response_data = json.dumps(
            [
                {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/ABC"},
                {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/DEF"},
            ]
        ).encode()

        with patch("cdp_client.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = response_data
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = lambda s, *a: None
            mock_urlopen.return_value = mock_response

            url = get_page_ws_url("http://127.0.0.1:9222", page_index=1)
            assert url == "ws://127.0.0.1:9222/devtools/page/DEF"
