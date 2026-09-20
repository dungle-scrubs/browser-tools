"""Unit tests for the parity suite's JSON-RPC-over-stdio broker.

The broker is driven through an injectable fake subprocess so these tests never
spawn chrome-devtools-mcp. The fake's stdout is a queue-backed iterator the test
feeds JSON response lines into; its stdin records every JSON-RPC line the broker
writes so the exact wire format is assertable.

These moved here with the broker (#91). They used to live in the MCP daemon
suite, which goes with the front (#92); the broker outlives it as the parity
gate's Node driver, so its tests have to outlive it too.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

import pytest
from node_broker import McpBroker


class _FakeStdout:
    """Queue-backed, line-iterable stand-in for a subprocess text stdout.

    The broker's reader iterates ``proc.stdout`` line by line; feeding a line
    here unblocks the next iteration. Feed ``None`` (via :meth:`close`) to
    signal EOF so the reader thread exits cleanly at teardown.
    """

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self) -> _FakeStdout:
        return self

    def __next__(self) -> str:
        item = self._lines.get()
        if item is None:
            raise StopIteration
        return item

    def feed(self, payload: dict[str, Any]) -> None:
        """Write a JSON-encoded response line for the reader to consume."""
        self._lines.put(json.dumps(payload) + "\n")

    def close(self) -> None:
        """Signal EOF so the reader thread stops blocking on the next read."""
        self._lines.put(None)


class _FakeStdin:
    """Thread-safe recorder of the JSON-RPC lines the broker writes to stdin."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writes: list[str] = []

    def write(self, text: str) -> int:
        with self._lock:
            self._writes.append(text)
            return len(text)

    def flush(self) -> None:
        """No-op flush; writes are recorded synchronously."""

    def lines(self) -> list[dict[str, Any]]:
        """Parse every complete JSON-RPC line written so far."""
        with self._lock:
            blob = "".join(self._writes)
        parsed: list[dict[str, Any]] = []
        for raw in blob.split("\n"):
            raw = raw.strip()
            if raw:
                parsed.append(json.loads(raw))
        return parsed


class _FakeProc:
    """Minimal subprocess double satisfying the broker's transport shape.

    ``stdout`` is a queue-backed iterator the test feeds JSON responses into;
    ``stdin`` records the broker's writes so tests can assert on them.
    """

    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin()

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        """No-op; the broker's terminate path is not exercised in these tests."""

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        """No-op; the broker's kill path is not exercised in these tests."""


@pytest.fixture
def fake_proc() -> _FakeProc:
    """Provide a fake subprocess the broker can multiplex without spawning."""
    proc = _FakeProc()
    yield proc
    proc.stdout.close()


class TestMcpBroker:
    """Unit tests for the JSON-RPC-over-stdio request multiplexer."""

    @staticmethod
    def _await_lines(fake_proc: _FakeProc, count: int, deadline: float) -> list[dict[str, Any]]:
        """Block until ``count`` request lines have been written to stdin."""
        while time.time() < deadline:
            lines = fake_proc.stdin.lines()
            if len(lines) >= count:
                return lines
            time.sleep(0.005)
        return fake_proc.stdin.lines()

    def test_two_requests_get_distinct_ids_and_route_correctly(self, fake_proc: _FakeProc) -> None:
        """Two concurrent requests get distinct incrementing ids and each
        caller receives its own response, not the other's."""
        broker = McpBroker(["fake"], proc=fake_proc)
        broker.start()

        results: dict[str, dict[str, Any]] = {}

        def call(tag: str, method: str) -> None:
            results[tag] = broker.request(method, {}, timeout=2.0)

        t1 = threading.Thread(target=call, args=("a", "method_a"))
        t2 = threading.Thread(target=call, args=("b", "method_b"))
        t1.start()
        t2.start()

        lines = self._await_lines(fake_proc, 2, time.time() + 2.0)
        wanted = {"method_a", "method_b"}

        # Answer each pending request with a response tagged by its method.
        for line in lines:
            if line.get("method") in wanted:
                fake_proc.stdout.feed(
                    {"jsonrpc": "2.0", "result": {"who": line["method"]}, "id": line["id"]}
                )

        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        # Each caller got its own response, proving correct per-id routing.
        assert results["a"]["result"]["who"] == "method_a"
        assert results["b"]["result"]["who"] == "method_b"
        # Internal ids were distinct and incrementing from 1.
        ids = sorted(ln["id"] for ln in lines if ln.get("method") in wanted)
        assert ids == [1, 2]

    def test_timeout_returns_error_envelope(self, fake_proc: _FakeProc) -> None:
        """A request with no response returns the fixed timeout error envelope."""
        broker = McpBroker(["fake"], proc=fake_proc)
        broker.start()

        response = broker.request("never_answered", {}, timeout=0.1)

        assert response == {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": "Timeout"},
            "id": 0,
        }

    def test_late_response_does_not_corrupt_next_request(self, fake_proc: _FakeProc) -> None:
        """A response arriving after its caller timed out is dropped, so a later
        request still resolves with its own response."""
        broker = McpBroker(["fake"], proc=fake_proc)
        broker.start()

        # First request times out; its pending entry is removed.
        timed = broker.request("times_out", {}, timeout=0.1)
        assert timed["error"]["message"] == "Timeout"

        # A late response for the now-abandoned id arrives. The reader should
        # find no pending queue for it and drop it silently.
        fake_proc.stdout.feed({"jsonrpc": "2.0", "result": {"late": True}, "id": 1})
        time.sleep(0.1)  # let the reader observe and drop the late response

        outcome: dict[str, Any] = {}

        def second() -> None:
            outcome["resp"] = broker.request("fresh", {}, timeout=2.0)

        t = threading.Thread(target=second)
        t.start()

        fresh_lines = [
            ln
            for ln in self._await_lines(fake_proc, 2, time.time() + 2.0)
            if ln.get("method") == "fresh"
        ]
        assert fresh_lines, "second request was never written"
        fresh_id = fresh_lines[0]["id"]
        fake_proc.stdout.feed(
            {
                "jsonrpc": "2.0",
                "result": {"content": [{"type": "text", "text": "fresh-ok"}]},
                "id": fresh_id,
            }
        )
        t.join(timeout=2.0)

        # If the late response had leaked into this caller, the result would be
        # {"late": True} instead of the fresh-ok text.
        assert outcome["resp"]["result"]["content"][0]["text"] == "fresh-ok"
        assert outcome["resp"]["id"] == fresh_id

    def test_request_writes_correct_jsonrpc_line(self, fake_proc: _FakeProc) -> None:
        """request() writes a well-formed JSON-RPC line with the internal id."""
        broker = McpBroker(["fake"], proc=fake_proc)
        broker.start()

        outcome: dict[str, Any] = {}

        def do_request() -> None:
            outcome["resp"] = broker.request(
                "initialize", {"protocolVersion": "2024-11-05"}, timeout=2.0
            )

        t = threading.Thread(target=do_request)
        t.start()

        written_lines = self._await_lines(fake_proc, 1, time.time() + 2.0)
        assert written_lines, "request was never written"
        written = written_lines[0]

        assert written == {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
            "id": 1,
        }

        # Unblock the caller so the thread can join cleanly.
        fake_proc.stdout.feed({"jsonrpc": "2.0", "result": {"ok": True}, "id": 1})
        t.join(timeout=2.0)
        assert outcome["resp"]["result"] == {"ok": True}
        assert outcome["resp"]["id"] == 1
