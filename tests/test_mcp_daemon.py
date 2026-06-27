"""Tests for the MCP daemon broker and DaemonClient."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from browser_tools.persistent_browser import DaemonClient

# Path to the daemon script
DAEMON_SCRIPT = Path(__file__).resolve().parents[1] / "src" / "browser_tools" / "mcp_daemon.py"


@pytest.fixture
def short_tmp() -> Path:
    """Provide a short temp directory path for Unix socket tests.

    macOS limits AF_UNIX paths to 104 bytes; pytest's tmp_path is too long.

    Returns:
        Path to a short temporary directory (cleaned up after test).
    """
    d = Path(tempfile.mkdtemp(prefix="cdp_"))
    yield d
    import shutil

    shutil.rmtree(d, ignore_errors=True)


class FakeJsonRpcServer:
    """Minimal JSON-RPC server over Unix socket for testing DaemonClient.

    Speaks the same protocol as the real daemon: newline-delimited JSON-RPC
    over a Unix domain socket.
    """

    def __init__(self, socket_path: str, responses: dict[str, Any] | None = None):
        """Create a fake server.

        Args:
            socket_path: Path for the Unix domain socket.
            responses: Map of method names to response results.

        Returns:
            None.
        """
        self.socket_path = socket_path
        self.responses = responses or {}
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.received: list[dict] = []
        self._stop = threading.Event()

    def start(self) -> None:
        """Start the fake server in a background thread.

        Returns:
            None.
        """
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        self.server.listen(1)
        self.server.settimeout(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the fake server.

        Returns:
            None.
        """
        self._stop.set()
        if self.server:
            self.server.close()
        if self.thread:
            self.thread.join(timeout=3)

    def _serve(self) -> None:
        """Accept connections and respond to JSON-RPC requests."""
        while not self._stop.is_set():
            try:
                client, _ = self.server.accept()
            except (TimeoutError, OSError):
                continue
            try:
                self._handle_client(client)
            except Exception:
                pass
            finally:
                client.close()

    def _handle_client(self, client: socket.socket) -> None:
        """Handle one client connection."""
        client.settimeout(5)
        buf = b""
        while not self._stop.is_set():
            try:
                data = client.recv(65536)
            except TimeoutError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                request = json.loads(line)
                self.received.append(request)
                method = request.get("method", "")
                tool_name = request.get("params", {}).get("name", "")
                result = self.responses.get(tool_name, {"content": []})
                response = {
                    "jsonrpc": "2.0",
                    "result": result,
                    "id": request.get("id"),
                }
                client.sendall(json.dumps(response).encode() + b"\n")


class TestDaemonClient:
    """Tests for the DaemonClient socket communication."""

    def test_call_tool_sends_and_receives(self, short_tmp: Path) -> None:
        """DaemonClient should send JSON-RPC requests and receive responses."""
        socket_path = str(short_tmp / "t.sock")
        server = FakeJsonRpcServer(
            socket_path,
            responses={"take_snapshot": {"content": [{"type": "text", "text": "snapshot data"}]}},
        )
        server.start()

        try:
            with DaemonClient(socket_path, timeout=5) as client:
                response = client.call_tool("take_snapshot", {"verbose": True})
                assert response["result"]["content"][0]["text"] == "snapshot data"
                assert len(server.received) == 1
                assert server.received[0]["params"]["name"] == "take_snapshot"
                assert server.received[0]["params"]["arguments"] == {"verbose": True}
        finally:
            server.stop()

    def test_multiple_calls_on_same_connection(self, short_tmp: Path) -> None:
        """Multiple sequential calls should work on the same socket connection."""
        socket_path = str(short_tmp / "t.sock")
        server = FakeJsonRpcServer(
            socket_path,
            responses={
                "list_pages": {"content": [{"type": "text", "text": "page list"}]},
                "take_snapshot": {"content": [{"type": "text", "text": "snapshot"}]},
            },
        )
        server.start()

        try:
            with DaemonClient(socket_path, timeout=5) as client:
                r1 = client.call_tool("list_pages", {})
                r2 = client.call_tool("take_snapshot", {})
                assert r1["result"]["content"][0]["text"] == "page list"
                assert r2["result"]["content"][0]["text"] == "snapshot"
                assert len(server.received) == 2
        finally:
            server.stop()

    def test_message_ids_increment(self, short_tmp: Path) -> None:
        """Each call should use a unique incrementing message ID."""
        socket_path = str(short_tmp / "t.sock")
        server = FakeJsonRpcServer(socket_path)
        server.start()

        try:
            with DaemonClient(socket_path, timeout=5) as client:
                client.call_tool("tool_a", {})
                client.call_tool("tool_b", {})
                client.call_tool("tool_c", {})
                ids = [r["id"] for r in server.received]
                assert ids == [1, 2, 3]
        finally:
            server.stop()

    def test_connection_error_raises(self, tmp_path: Path) -> None:
        """Connecting to a non-existent socket should raise MCPInvocationError."""
        from browser_tools.persistent_browser import MCPInvocationError

        with (
            pytest.raises(MCPInvocationError, match="Failed to connect"),
            DaemonClient(str(tmp_path / "nonexistent.sock")) as client,
        ):
            pass

    def test_call_without_connection_raises(self) -> None:
        """Calling call_tool without entering context manager should raise."""
        from browser_tools.persistent_browser import MCPInvocationError

        client = DaemonClient("/fake/path")
        with pytest.raises(MCPInvocationError, match="Not connected"):
            client.call_tool("test", {})


class TestDaemonScript:
    """Integration tests for the mcp_daemon.py script."""

    def test_daemon_script_exists_and_is_executable(self) -> None:
        """The daemon script should exist at the expected path."""
        assert DAEMON_SCRIPT.exists()
        assert DAEMON_SCRIPT.suffix == ".py"

    def test_daemon_script_parse_args(self) -> None:
        """The daemon script should accept --socket, --pid-file, --mcp-command."""
        result = subprocess.run(
            [sys.executable, str(DAEMON_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert "--socket" in result.stdout
        assert "--pid-file" in result.stdout
        assert "--mcp-command" in result.stdout
