"""Bounded external CDP connections, including Chrome's approval handshake."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .core.cdp_client import CDPClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .endpoint import ResolvedEndpoint

APPROVAL_TIMEOUT = 30.0
APPROVAL_MESSAGE = (
    "No approval received in 30s. Chrome asks permission for each debugging "
    "connection; click Allow in the browser window."
)


class _DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # A redirect must not turn a validated loopback address into a remote one.
        return exc


class _ExternalClient(CDPClient):
    async def connect(self) -> None:
        # Adapt the vendored client's transport setup without changing its source.
        self._ws = await _DirectConnect(  # pyright: ignore[reportPrivateUsage]
            self._ws_url,
            open_timeout=APPROVAL_TIMEOUT,
            close_timeout=0,  # pyright: ignore[reportPrivateUsage]
            proxy=None,
            max_size=50 * 1024 * 1024,
        )
        self._connected = True
        self._recv_task = asyncio.create_task(self._recv_loop())  # pyright: ignore[reportPrivateUsage]


async def _verified_client(url: str) -> CDPClient:
    client = _ExternalClient(url)
    try:
        await client.connect()
        result: dict[str, Any] = await client.send("Browser.getVersion")
        if not all(isinstance(result.get(key), str) for key in ("protocolVersion", "product")):
            raise ConnectionError("endpoint did not return a DevTools Browser.getVersion response")
        return client
    except BaseException:
        await client.close()
        raise


@contextlib.asynccontextmanager
async def external_browser_connection(endpoint: ResolvedEndpoint) -> AsyncGenerator[CDPClient]:
    """Race loopback families within one bound, then keep the verified connection."""
    tasks = [asyncio.create_task(_verified_client(url)) for url in endpoint.websocket_urls]
    client: CDPClient | None = None
    failures: list[BaseException] = []
    try:
        try:
            async with asyncio.timeout(APPROVAL_TIMEOUT):
                pending = set(tasks)
                while pending and client is None:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            candidate = task.result()
                        except Exception as exc:
                            failures.append(exc)
                        else:
                            if client is None:
                                client = candidate
                if client is None:
                    if any(isinstance(exc, TimeoutError) for exc in failures):
                        raise TimeoutError
                    if any(
                        isinstance(exc, InvalidStatus) and exc.response.status_code == 403
                        for exc in failures
                    ):
                        raise ConnectionError(
                            "Chrome did not permit the debugging connection (HTTP 403). "
                            "Check Allow and chrome://policy: RemoteDebuggingAllowed "
                            "(devtools.remote_debugging.allowed) may prohibit debugging."
                        )
                    raise ConnectionError(
                        f"No DevTools endpoint answered on port {endpoint.port}: "
                        + "; ".join(str(exc) for exc in failures)
                    )
        except TimeoutError as exc:
            raise ConnectionError(APPROVAL_MESSAGE) from exc
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, CDPClient) and result is not client:
                await result.close()
        yield client
    except (OSError, WebSocketException) as exc:
        raise ConnectionError(str(exc)) from exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, CDPClient):
                await result.close()
