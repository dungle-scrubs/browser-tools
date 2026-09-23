"""Bounded external CDP connections, including Chrome's approval handshake.

Every wait on an external peer has a number, and the numbers are here.

The first version bounded only the opening handshake. A peer that completed the
WebSocket handshake, answered ``Browser.getVersion`` with a plausible
``protocolVersion`` and ``product``, and then never answered again left
``Runtime.evaluate`` running past two minutes and ``attach`` running past ninety
seconds, because every send after the handshake was unbounded and the vendored
client defaults ``timeout`` to ``None``. That is not only an adversarial peer:
bt's own message for a bound-but-silent port says "busy or hung browser UI
thread", so a wedged real Chrome reaches the same state.

Four bounds, each with its own job:

- :data:`APPROVAL_TIMEOUT` covers the WebSocket handshake and the one
  ``Browser.getVersion`` that proves the peer is a browser. It is 30 seconds
  because that is how long a person gets to see Chrome's per-connection prompt
  and click Allow, and it is the number the guide quotes.
- :data:`SESSION_TIMEOUT` covers opening a page session: ``Target.getTargets``
  and ``Target.attachToTarget`` together. Both are answered by the browser
  process without a renderer, in milliseconds on a real Chrome, so 30 seconds
  is three orders of magnitude of headroom and still a bound.
- :data:`COMMAND_TIMEOUT` is the backstop under every other CDP command. It is
  larger, 120 seconds, because these reach a renderer doing real work: a
  navigation that commits slowly, a full-page capture, a heap snapshot. It
  bounds one command's reply and not the verb: a verb that waits on events,
  such as ``wait`` or ``console-list --duration``, keeps its own deadline and
  is unaffected.
- :data:`TEARDOWN_TIMEOUT` covers ``Target.detachFromTarget`` on a connection
  that is about to close anyway. It is small, because a verb that has already
  produced its answer must not then wait on a peer that has stopped talking.

One peer, one connection. The first version raced both loopback families and
kept whichever answered first, which sent the port file's browser GUID -- an
unauthenticated capability token -- to whatever held the other family, and made
the choice of browser a coin flip. ``chrome_discovery`` now verifies which
family is Chrome before anything is dialled, so this module receives exactly
one URL. On a Chrome that asks for approval that is also one Allow prompt for
one invocation, which is what the guide has always said.
"""

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
SESSION_TIMEOUT = 30.0
COMMAND_TIMEOUT = 120.0
TEARDOWN_TIMEOUT = 5.0


def no_answer_message(endpoint: ResolvedEndpoint, seconds: float) -> str:
    """Say what is known about a peer that accepted a connection and went quiet.

    What is established: the address accepted a TCP connection and did not
    finish the exchange within the bound. What is not established is why. At
    least three causes produce it, and they have different remedies, so the
    message names them and names the check rather than asserting one:

    - Chrome is waiting for the person to click Allow on its per-connection
      prompt, which only happens on a Chrome that asks.
    - The browser is there but wedged. Chrome serves DevTools from its UI
      thread, so a busy or hung UI thread accepts and never answers.
    - Something that is not a browser holds the port. Two plain TCP listeners,
      with no Chrome anywhere, produced exactly this outcome.

    The earlier text asserted the first cause for all three.
    """
    from .endpoint import describe_endpoint

    return (
        f"{endpoint.flag} connected to {endpoint.host}:{endpoint.port} but got no "
        f"DevTools answer in {seconds:.0f}s. What that means is not decided by this "
        f"timeout. Chrome asks permission for each debugging connection, so look for "
        f"an Allow prompt in the browser window. A browser whose UI thread is busy or "
        f"hung accepts the connection and never answers. A process that is not a "
        f"browser does the same.\n"
        f"{describe_endpoint(endpoint.port)}"
    )


def stopped_answering_message(endpoint: ResolvedEndpoint) -> str:
    """Say that a verified DevTools peer went quiet part way through.

    Different from :func:`no_answer_message`: this peer did complete the
    handshake and did answer ``Browser.getVersion``, so approval is settled and
    the remaining causes are a wedged browser or a peer that only imitates one.
    """
    from .endpoint import describe_endpoint

    return (
        f"The browser at {endpoint.host}:{endpoint.port} answered Browser.getVersion "
        f"and then stopped answering. bt does not wait indefinitely on a CDP command. "
        f"A browser whose UI thread is busy or hung behaves this way, and so does a "
        f"peer that imitates DevTools without implementing it.\n"
        f"{describe_endpoint(endpoint.port)}"
    )


class _DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # A redirect must not turn a validated loopback address into a remote one.
        return exc


class ExternalClient(CDPClient):
    """The vendored client with a transport bt controls and a bound on every send.

    ``CDPClient.send`` defaults ``timeout`` to ``None``, which is the right
    default for a browser bt launched and supervises and the wrong one for an
    address someone typed. Overriding it here puts the bound under every caller
    -- each verb, the curated runtime, the Step Run, and the teardown in
    ``one_shot`` -- without editing the verbatim core and without every call
    site having to remember a number.
    """

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

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one command, never without a bound."""
        if timeout is None:
            timeout = COMMAND_TIMEOUT
        return await super().send(
            method=method, params=params, session_id=session_id, timeout=timeout
        )


async def _verified_client(url: str) -> CDPClient:
    client = ExternalClient(url)
    try:
        await client.connect()
        result: dict[str, Any] = await client.send("Browser.getVersion", timeout=APPROVAL_TIMEOUT)
        if not all(isinstance(result.get(key), str) for key in ("protocolVersion", "product")):
            raise ConnectionError("endpoint did not return a DevTools Browser.getVersion response")
        return client
    except BaseException:
        await client.close()
        raise


@contextlib.asynccontextmanager
async def external_browser_connection(endpoint: ResolvedEndpoint) -> AsyncGenerator[CDPClient]:
    """Open the one verified connection to an external browser, within one bound."""
    url = endpoint.websocket_url
    if url is None:
        raise ConnectionError(f"{endpoint.flag} resolved no browser WebSocket URL")
    # Run the handshake in its own task. When the bound expires this task is
    # the one cancelled, and the connect attempt is cancelled afterwards from a
    # scope that is no longer cancelling, so its own cleanup can still await.
    task = asyncio.create_task(_verified_client(url))
    client: CDPClient | None = None
    try:
        try:
            async with asyncio.timeout(APPROVAL_TIMEOUT):
                done, _ = await asyncio.wait({task})
                client = next(iter(done)).result()
        except TimeoutError as exc:
            raise ConnectionError(no_answer_message(endpoint, APPROVAL_TIMEOUT)) from exc
        except InvalidStatus as exc:
            if exc.response.status_code == 403:
                raise ConnectionError(
                    "Chrome did not permit the debugging connection (HTTP 403). "
                    "Check Allow and chrome://policy: RemoteDebuggingAllowed "
                    "(devtools.remote_debugging.allowed) may prohibit debugging."
                ) from exc
            raise ConnectionError(
                f"{endpoint.flag} got HTTP {exc.response.status_code} from "
                f"{endpoint.host}:{endpoint.port} instead of a WebSocket handshake."
            ) from exc
        except ConnectionError as exc:
            raise ConnectionError(
                f"No DevTools endpoint answered on port {endpoint.port}: {exc}"
            ) from exc
        try:
            yield client
        except TimeoutError as exc:
            raise ConnectionError(stopped_answering_message(endpoint)) from exc
    except (OSError, WebSocketException) as exc:
        raise ConnectionError(str(exc)) from exc
    finally:
        if not task.done():
            task.cancel()
        for result in await asyncio.gather(task, return_exceptions=True):
            if isinstance(result, CDPClient):
                await result.close()
