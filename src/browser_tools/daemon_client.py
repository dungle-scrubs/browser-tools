"""Daemon client for connecting to the long-lived MCP daemon over a Unix
domain socket.

Used as a drop-in replacement for ChromeMcpSession in the persistent
browser path.
"""

from __future__ import annotations

import json
import socket
from typing import Any

from .chrome_utils import MCPInvocationError

DEFAULT_BROWSER_TIMEOUT_SECONDS = 60


class DaemonClient:
    """Client that connects to the MCP daemon over a Unix domain socket.

    Used as a drop-in replacement for ChromeMcpSession in the persistent
    browser path.  Supports the same context-manager and call_tool interface
    but routes requests to the long-lived daemon instead of spawning a
    fresh MCP subprocess.
    """

    def __init__(self, socket_path: str, timeout: int = DEFAULT_BROWSER_TIMEOUT_SECONDS):
        """Create a daemon client.

        Args:
            socket_path: Path to the daemon's Unix domain socket.
            timeout: Maximum seconds to wait for each RPC response.

        Returns:
            None.
        """
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._msg_id = 0
        self._buf = b""

    def __enter__(self) -> DaemonClient:
        """Connect to the daemon socket.

        Returns:
            Connected DaemonClient instance.

        Raises:
            MCPInvocationError: If the socket connection fails.
        """
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.connect(self.socket_path)
        except OSError as exc:
            self._sock.close()
            self._sock = None
            raise MCPInvocationError(f"Failed to connect to MCP daemon: {exc}") from exc
        self._sock.settimeout(self.timeout)
        self._buf = b""
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, traceback: Any) -> None:
        """Close the socket connection.

        Args:
            exc_type: Exception type from context manager protocol.
            exc: Exception instance from context manager protocol.
            traceback: Traceback from context manager protocol.

        Returns:
            None.
        """
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Send a tool call to the daemon and return the response.

        Args:
            name: MCP tool name.
            arguments: Tool arguments.

        Returns:
            Raw JSON-RPC response from the MCP server.

        Raises:
            MCPInvocationError: If the request fails or times out.
        """
        if self._sock is None:
            raise MCPInvocationError("Not connected to MCP daemon")

        self._msg_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": self._msg_id,
        }

        try:
            self._sock.sendall(json.dumps(request).encode() + b"\n")
        except OSError as exc:
            raise MCPInvocationError(f"Failed to send request to MCP daemon: {exc}") from exc

        while b"\n" not in self._buf:
            try:
                data = self._sock.recv(262144)
            except TimeoutError as exc:
                raise MCPInvocationError(
                    f"MCP daemon request timed out after {self.timeout}s"
                ) from exc
            except OSError as exc:
                raise MCPInvocationError(f"MCP daemon connection error: {exc}") from exc
            if not data:
                raise MCPInvocationError("MCP daemon connection closed unexpectedly")
            self._buf += data

        line, self._buf = self._buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPInvocationError(f"Invalid JSON from MCP daemon: {exc}") from exc
