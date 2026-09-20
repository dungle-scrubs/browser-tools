"""Chrome discovery must not block the event loop.

CDP discovery is stdlib ``urllib`` over HTTP: synchronous, and slow whenever
Chrome is slow to answer. Called straight from an ``async def``, it parks the
only thread the event loop has, so every other coroutine and timer on that
loop stalls for the duration. Reported as #31 against
``CDPHandler._connect_cdp``; the same shape appears at every async call site
of a discovery helper, so the guard here is a whole-tree scan.
"""

from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# Helpers that reach Chrome over synchronous HTTP, or block on a socket.
# Calling one of these from an async function stalls the loop.
BLOCKING_HELPERS = frozenset(
    {
        "check_cdp_port",
        "enumerate_tabs",
        "fetch_protocol_schema",
        "get_browser_ws_url",
        "get_page_ws_url",
        "get_targets",
        "get_ws_url",
        "is_devtools_available",
        "urlopen",
    }
)

RESPONSE_DELAY_SECONDS = 0.25
TICK_SECONDS = 0.01
# The loop is blocked if a tick lands later than this. Well under the response
# delay, well over ordinary scheduler jitter on a loaded machine.
MAX_TOLERABLE_LAG_SECONDS = 0.1


class _BlockingCallFinder(ast.NodeVisitor):
    """Collect blocking-helper calls whose nearest enclosing def is async."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.offenders: list[str] = []
        self._enclosing: list[ast.AST] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enclosing.append(node)
        self.generic_visit(node)
        self._enclosing.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enclosing.append(node)
        self.generic_visit(node)
        self._enclosing.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        enclosing = self._enclosing[-1] if self._enclosing else None
        if name in BLOCKING_HELPERS and isinstance(enclosing, ast.AsyncFunctionDef):
            self.offenders.append(
                f"{self.where}:{node.lineno} calls {name}() "
                f"inside async def {enclosing.name}"
            )
        self.generic_visit(node)


def _blocking_calls_inside_async_functions() -> list[str]:
    """Return one description per blocking helper call made from an async def."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        finder = _BlockingCallFinder(str(path.relative_to(SRC)))
        finder.visit(ast.parse(path.read_text()))
        offenders.extend(finder.offenders)
    return offenders


class _SlowDevToolsHandler(BaseHTTPRequestHandler):
    """A Chrome DevTools endpoint that answers slowly."""

    def do_GET(self) -> None:
        time.sleep(RESPONSE_DELAY_SECONDS)
        body = json.dumps(
            [
                {
                    "type": "page",
                    "url": "https://example.com/",
                    "title": "Example",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/SLOW",
                }
            ]
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def slow_devtools_port() -> object:
    """Serve a delayed DevTools endpoint on an ephemeral loopback port."""
    server = HTTPServer(("127.0.0.1", 0), _SlowDevToolsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _worst_tick_lag(during: object) -> float:
    """Run ``during`` and return the worst late arrival of a fixed-rate timer."""
    worst = 0.0
    stop = False

    async def tick() -> None:
        nonlocal worst
        while not stop:
            before = time.perf_counter()
            await asyncio.sleep(TICK_SECONDS)
            worst = max(worst, time.perf_counter() - before - TICK_SECONDS)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(TICK_SECONDS * 3)  # let the ticker settle
    try:
        await during
    finally:
        stop = True
        await ticker
    return worst


class TestDiscoveryIsNonBlocking:
    def test_no_async_function_calls_a_blocking_helper(self) -> None:
        """Every async call site must use the awaitable discovery helper."""
        offenders = _blocking_calls_inside_async_functions()
        assert offenders == [], "blocking Chrome discovery on an async path:\n" + "\n".join(
            offenders
        )

    @pytest.mark.asyncio
    async def test_get_ws_url_async_leaves_the_loop_free(self, slow_devtools_port: int) -> None:
        """A slow endpoint must not stall coroutines sharing the loop."""
        from browser_tools.core.cdp_client import get_ws_url_async

        lag = await _worst_tick_lag(get_ws_url_async(port=slow_devtools_port))

        assert lag < MAX_TOLERABLE_LAG_SECONDS, (
            f"event loop stalled {lag:.3f}s while discovery was pending"
        )

    @pytest.mark.asyncio
    async def test_get_ws_url_async_returns_the_same_url(self, slow_devtools_port: int) -> None:
        """The awaitable helper must agree with the synchronous one."""
        from browser_tools.core.cdp_client import get_ws_url, get_ws_url_async

        expected = await asyncio.to_thread(get_ws_url, port=slow_devtools_port)
        assert await get_ws_url_async(port=slow_devtools_port) == expected

    @pytest.mark.asyncio
    async def test_get_page_ws_url_async_leaves_the_loop_free(
        self, slow_devtools_port: int
    ) -> None:
        """The helper behind CDPHandler._connect_cdp, the path #31 reported."""
        from browser_tools.cdp_client import get_page_ws_url_async

        browser_url = f"http://127.0.0.1:{slow_devtools_port}"
        lag = await _worst_tick_lag(get_page_ws_url_async(browser_url))

        assert lag < MAX_TOLERABLE_LAG_SECONDS, (
            f"event loop stalled {lag:.3f}s while discovery was pending"
        )

    @pytest.mark.asyncio
    async def test_get_ws_url_async_propagates_failure(self) -> None:
        """A closed port must still raise ConnectionError, not return None."""
        from browser_tools.core.cdp_client import get_ws_url_async

        with pytest.raises(ConnectionError):
            await get_ws_url_async(port=1)
