"""The handler's client, backed by an attached Target session.

RFC-03 requires a Step Run to hold one browser-level CDP connection with one
flattened ``Target`` session, and requires the run's **CDPRuntime, and
therefore its frame manager, to be built over that attached session** rather
than over a page-level WebSocket. That is a change to how ``CDPHandler``
connects, and the RFC states it as a contract requirement without phasing.
This module is the first phase of meeting it.

Why an adapter rather than a rewrite
------------------------------------
Two clients exist and they are not interchangeable:

- ``cdp_client.CDPClient`` opens a **page-level** socket, sends with no
  ``sessionId``, and registers event handlers with no session filter. The
  handler and everything under it use this one.
- ``core.cdp_client.CDPClient`` opens a **browser-level** socket and carries a
  ``sessionId`` on every send and every event. ``one_shot_page_session`` uses
  this one.

The difference is not cosmetic. ``core/cdp_client.py`` dispatches an event to
a callback only when the callback's filter session is ``None`` or equal to the
event's ``sessionId``, so a page-level connection and a session-filtered
subscriber never meet.

Converging them by threading a ``session_id`` through the handler would touch
about forty call sites across ``cdp_handler``, ``frame_manager``,
``screencast``, ``curated`` and ``interstitial``, in one change, with no
intermediate state that runs. This class instead presents the page-level
client's surface over the browser-level one, so the call sites do not move and
the transport underneath them does. What each caller sends is unchanged; where
it is sent changes.

What this preserves deliberately
--------------------------------
- **The command timeout.** ``core.cdp_client.CDPClient.send`` defaults to no
  deadline, so a command Chrome never answers hangs forever. The page-level
  client bounds every command at 30 seconds. ``send`` here asks the core
  client for that bound on every command.

  The bound is passed down rather than applied with a ``wait_for`` around the
  call, and that is not a style choice. Only the core client's ``send`` knows
  the message id, so only it can retire the pending entry when the wait ends
  early. A ``wait_for`` out here leaves the entry behind, and the late
  response then lands on a cancelled future and takes the receive loop with
  it.
- **The exception type.** ``cdp_handler`` and ``screencast`` both catch
  ``cdp_client.CDPError``. The core client raises ``core.errors.CDPError``,
  an unrelated class, and a ``ConnectionError`` when the socket is gone. Both
  are translated, so every existing ``except CDPError`` still fires.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .cdp_client import CDPError
from .core.errors import CDPError as CoreCDPError

if TYPE_CHECKING:
    from .core.cdp_client import CDPClient as CoreCDPClient

logger = logging.getLogger(__name__)

#: Matches ``cdp_client.CDPClient``'s default. A command that never returns is
#: a hung invocation, and the page-level client has always bounded it.
DEFAULT_TIMEOUT_SECONDS = 30.0


class AttachedSessionClient:
    """``cdp_client.CDPClient``'s surface over one attached Target session.

    Wraps a connected ``core.cdp_client.CDPClient`` and the ``sessionId`` of a
    flattened ``Target`` session on it. Every send carries that session, and
    every event subscription filters on it.

    The lifetime of the connection and the session belongs to whoever opened
    them, which is ``one_shot_page_session``. This object never closes either.
    """

    def __init__(self, client: CoreCDPClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._closed = False

    @property
    def session_id(self) -> str:
        """The attached session every send and subscription carries."""
        return self._session_id

    @property
    def connected(self) -> bool:
        """Whether the underlying connection is usable.

        ``CDPHandler.available`` reads this, and ``curated`` polls that to
        decide the handler came up, so it has to mean the same thing it did
        on the page-level client.
        """
        return not self._closed and bool(getattr(self._client, "_connected", False))

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one command over the attached session and return its result.

        Signature and return shape match ``cdp_client.CDPClient.send``, so the
        call sites do not change. ``CDPError`` is raised for a protocol error,
        a lost connection, and a timeout, because that is the one exception
        those call sites catch.
        """
        if self._closed:
            raise CDPError("Not connected to Chrome CDP")
        deadline = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            return await self._client.send(
                method=method,
                params=params,
                session_id=self._session_id,
                timeout=deadline,
            )
        except CoreCDPError as exc:
            raise CDPError(f"CDP error in {method}: {exc.message}") from exc
        except TimeoutError as exc:
            raise CDPError(f"CDP command timed out after {deadline}s: {method}") from exc
        except ConnectionError as exc:
            raise CDPError(f"Failed to send CDP command: {exc}") from exc

    def on(self, event: str, handler: Any) -> None:
        """Subscribe to an event on this session only.

        The session filter is the whole point: without it a subscriber on a
        browser-level connection would also see the same event from every
        other attached target.
        """
        self._client.on(event, handler, session_id=self._session_id)

    def off(self, event: str, handler: Any) -> None:
        """Unsubscribe. A handler that was never registered is ignored."""
        self._client.off(event, handler)

    async def disconnect(self) -> None:
        """Mark this client unusable without touching the connection.

        The connection and the session are owned by the context manager that
        opened them. Closing them here would detach a session that manager is
        about to detach again, and close a socket it still holds.
        """
        self._closed = True


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "AttachedSessionClient"]
