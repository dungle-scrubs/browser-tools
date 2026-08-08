"""MCP request broker: JSON-RPC-over-stdio multiplexer for the MCP subprocess.

Owns the chrome-devtools-mcp subprocess and multiplexes JSON-RPC requests from
many callers onto its single stdin/stdout pair. Each request gets a fresh
internal id; the matching response is routed back to the waiting caller through
a per-id queue. A timeout never leaves a dangling pending entry, and a response
that arrives after its caller has timed out is dropped instead of corrupting a
later request.

The subprocess handle is injectable so tests can drive the broker with a fake
process whose stdout is a text-iterable source the test feeds JSON lines into
and whose stdin captures writes, without spawning chrome-devtools-mcp.
"""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
from typing import IO, Any, Protocol


class _McpTransport(Protocol):
    """Structural shape of the subprocess the broker multiplexes.

    A ``subprocess.Popen[str]`` opened with ``text=True`` satisfies this in
    production; tests pass a fake whose ``stdin`` captures writes and whose
    ``stdout`` is a text-iterable source fed by the test.
    """

    stdout: IO[str] | None
    stdin: IO[str] | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class McpBroker:
    """Multiplex JSON-RPC requests onto one MCP subprocess over stdio.

    Owns the subprocess, an incrementing request-id counter, a map of pending
    ids to per-caller response queues, the lock guarding them, and the stdout
    reader thread that routes each response back to its caller.
    """

    def __init__(
        self,
        command: list[str],
        *,
        proc: _McpTransport | None = None,
    ) -> None:
        """Create a broker.

        When ``proc`` is None the broker spawns ``command`` itself; otherwise it
        adopts the passed subprocess (for tests). The reader thread is not
        started here - call :meth:`start` once wiring is complete.

        Args:
            command: Command vector used to spawn the MCP subprocess when
                ``proc`` is None.
            proc: Optional pre-built subprocess. Production passes None; tests
                pass a fake whose stdout/stdin are controllable.

        Returns:
            None.
        """
        if proc is None:
            self._proc: _McpTransport = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        else:
            self._proc = proc

        self._msg_id_counter = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        """Start the stdout reader daemon thread.

        Returns:
            None.
        """
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        """Route JSON-RPC responses from MCP stdout to waiting callers.

        Iterates the subprocess stdout line by line, parses each line as JSON,
        and delivers the payload to the queue registered for its ``id``. A
        response whose id has no pending queue (the caller already timed out
        and popped it) is dropped.
        """
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp_id = payload.get("id")
            with self._lock:
                resp_queue = self._pending.get(resp_id)
            if resp_queue is not None:
                resp_queue.put(payload)

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request to the MCP subprocess and wait for the reply.

        The broker uses its own internal id namespace, so callers must reattach
        their client id onto the returned response if they need it preserved.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.
            timeout: Seconds to wait for a response.

        Returns:
            JSON-RPC response dict. On timeout returns the envelope
            ``{"jsonrpc": "2.0", "error": {"code": -32000, "message":
            "Timeout"}, "id": 0}`` with the pending entry already removed.
        """
        with self._lock:
            self._msg_id_counter += 1
            internal_id = self._msg_id_counter
            resp_q: queue.Queue[dict[str, Any]] = queue.Queue()
            self._pending[internal_id] = resp_q

        request_payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": internal_id,
        }
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(request_payload) + "\n")
        self._proc.stdin.flush()

        try:
            response = resp_q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(internal_id, None)
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Timeout"},
                "id": 0,
            }

        with self._lock:
            self._pending.pop(internal_id, None)
        return response

    def is_alive(self) -> bool:
        """Return whether the subprocess is still running."""
        return self._proc.poll() is None

    def terminate(self) -> None:
        """Best-effort reap of the subprocess: terminate, wait, then kill.

        Returns:
            None.
        """
        with contextlib.suppress(Exception):
            self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                self._proc.kill()
                self._proc.wait(timeout=5)
        except Exception:
            pass
