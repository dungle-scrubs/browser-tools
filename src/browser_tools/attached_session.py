"""The handler's client, backed by an attached Target session.

RFC-03 requires the run's **CDPRuntime**, and therefore its frame manager, to
sit on one flattened ``Target`` session of one browser-level CDP connection.
``AttachedSessionClient`` is what puts it there: it presents
``cdp_client.CDPClient``'s surface over ``core.cdp_client.CDPClient``, so what
a caller sends is unchanged and where it is sent changes.

Three properties the callers depend on, none of which the core client gives
on its own:

- **Session scope.** Every send carries the session, and every subscription
  filters on it. A browser-level connection sees every attached target's
  events; ``core/cdp_client.py`` delivers one only when the subscriber's
  filter session matches.
- **A bounded command.** The core client defaults to no deadline. ``send``
  asks it for 30 seconds, the bound the page-level client always applied. The
  bound is passed down, not wrapped around the call: only the core ``send``
  knows the message id, so only it can retire the pending entry when the wait
  ends early. A ``wait_for`` out here leaves the entry behind, and the late
  response lands on a cancelled future and kills the receive loop.
- **One exception type.** ``cdp_handler`` and ``screencast`` catch
  ``cdp_client.CDPError``. The core client raises ``core.errors.CDPError``, an
  unrelated class, and a bare ``ConnectionError``. Both are translated.

Ownership: ``one_shot_page_session`` opens the connection and the session and
closes both. This object closes neither.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .cdp_client import CDPError
from .core.errors import CDPError as CoreCDPError

if TYPE_CHECKING:
    from .core.cdp_client import CDPClient as CoreCDPClient

logger = logging.getLogger(__name__)

#: Matches ``cdp_client.CDPClient``'s default.
DEFAULT_TIMEOUT_SECONDS = 30.0


class AttachedSessionClient:
    """``cdp_client.CDPClient``'s surface over one attached Target session."""

    def __init__(self, client: CoreCDPClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._closed = False
        # Every handler this client registered, in order, so `off` can hand
        # the core client back the same object it was given. See `off`.
        self._subscriptions: list[tuple[str, Any]] = []

    @property
    def session_id(self) -> str:
        """The attached session every send and subscription carries."""
        return self._session_id

    @property
    def raw(self) -> CoreCDPClient:
        """The browser-level client underneath, for a caller that needs it.

        A Step Run executes the One-Shot Session verbs by calling their
        ``*_on_session`` coroutines, which take the core client and a
        ``sessionId`` rather than this adapter. They get them from here, so
        every step still runs on the run's one session.
        """
        return self._client

    @property
    def connected(self) -> bool:
        """Whether the underlying connection is usable."""
        return not self._closed and bool(getattr(self._client, "_connected", False))

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one command over the attached session and return its result.

        Raises ``CDPError`` for a protocol error, a lost connection and a
        timeout alike.
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
        """Subscribe to an event on this session only."""
        self._subscriptions.append((event, handler))
        self._client.on(event, handler, session_id=self._session_id)

    def off(self, event: str, handler: Any) -> None:
        """Unsubscribe. A handler that was never registered is ignored.

        Matching is by equality, which is what the page-level client did and
        what callers rely on. The core client matches by identity, and
        ``obj.method`` builds a fresh bound method on every access: equal to
        the one registered, never the same object. ``ScreencastRecorder``
        subscribes with ``self.on_frame`` and unsubscribes with
        ``self.on_frame``, so identity matching would leave the subscription
        in place and a reused recorder would double every frame.
        """
        for index, (registered_event, registered) in enumerate(self._subscriptions):
            if registered_event == event and registered == handler:
                del self._subscriptions[index]
                self._client.off(event, registered)
                return
        self._client.off(event, handler)

    async def disconnect(self) -> None:
        """Mark this client unusable. The connection is not ours to close."""
        self._closed = True


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "AttachedSessionClient"]
