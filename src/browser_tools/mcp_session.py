"""Short-lived MCP session wrapper for browser-tools.

Extracted from persistent_browser.py to keep the module under 800 lines.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any

from .chrome_utils import MCPInvocationError

DEFAULT_BROWSER_TIMEOUT_SECONDS = 60


class ChromeMcpSession:
    """Short-lived MCP session used for one wrapper invocation.

    Why this exists: the browser itself persists across wrapper invocations, but
    the MCP server is restarted per wrapper call. This class keeps the MCP server
    alive long enough to restore page selection and run the requested tool in the
    same session.
    """

    def __init__(self, command: list[str], timeout_seconds: int = DEFAULT_BROWSER_TIMEOUT_SECONDS):
        """Create an MCP session wrapper.

        Args:
            command: Subprocess command for chrome-devtools-mcp.
            timeout_seconds: Maximum time to wait for individual RPC responses.

        Returns:
            None.
        """
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.msg_id = 0
        self.pending_responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

    def __enter__(self) -> ChromeMcpSession:
        """Start the subprocess and initialize the MCP session.

        Returns:
            The running ChromeMcpSession instance.

        Raises:
            MCPInvocationError: If the subprocess cannot be started or initialized.
        """
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MCPInvocationError(
                "npx command not found. Please ensure Node.js is installed."
            ) from exc
        except OSError as exc:
            raise MCPInvocationError(f"Failed to start chrome-devtools-mcp: {exc}") from exc

        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        self._initialize()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: object) -> None:
        """Terminate the subprocess and release resources.

        Args:
            exc_type: Exception type passed by the context manager protocol.
            exc: Exception instance passed by the context manager protocol.
            traceback: Traceback passed by the context manager protocol.

        Returns:
            None.
        """
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one MCP tool in the active session.

        Args:
            name: MCP tool name.
            arguments: Tool arguments.

        Returns:
            Raw JSON-RPC response from the MCP server.
        """
        return self._send_request("tools/call", {"name": name, "arguments": arguments})

    def _initialize(self) -> None:
        """Perform the MCP initialize handshake.

        Returns:
            None.

        Raises:
            MCPInvocationError: If initialization fails.
        """
        response = self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "browser-tools-session", "version": "1.0.0"},
            },
        )
        if "error" in response:
            raise MCPInvocationError(
                f"Failed to initialize browser-tools MCP session: {response['error']}"
            )

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response.

        Args:
            method: JSON-RPC method name.
            params: JSON-RPC method parameters.

        Returns:
            JSON-RPC response payload.

        Raises:
            MCPInvocationError: If the request cannot be sent or times out.
        """
        if not self.process or not self.process.stdin:
            raise MCPInvocationError("browser-tools MCP session is not running")

        self.msg_id += 1
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending_responses[self.msg_id] = response_queue
        request = {"jsonrpc": "2.0", "method": method, "params": params, "id": self.msg_id}

        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            self.pending_responses.pop(self.msg_id, None)
            raise MCPInvocationError(
                f"Failed to write request to browser-tools MCP session: {exc}"
            ) from exc

        try:
            response = response_queue.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            self.pending_responses.pop(self.msg_id, None)
            raise MCPInvocationError(
                f"browser-tools MCP session timed out after {self.timeout_seconds} seconds"
            ) from exc

        self.pending_responses.pop(self.msg_id, None)
        return response

    def _read_stdout(self) -> None:
        """Route JSON-RPC responses from stdout to waiting requests.

        Returns:
            None.
        """
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = payload.get("id")
            if response_id in self.pending_responses:
                self.pending_responses[response_id].put(payload)

    def _read_stderr(self) -> None:
        """Consume stderr to avoid blocking on a full pipe.

        Returns:
            None.
        """
        if not self.process or not self.process.stderr:
            return
        for _line in self.process.stderr:
            continue
