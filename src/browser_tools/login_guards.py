"""Keep dialog contents and CDP frames out of login diagnostics."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def quiet_websocket_logs() -> Generator[None]:
    """Suppress value-bearing frame logs until the login socket closes."""
    root = logging.getLogger("websockets")
    saved: list[tuple[logging.Logger, int]] = [(root, root.level)]
    root.setLevel(logging.WARNING)
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if name.startswith("websockets.") and isinstance(obj, logging.Logger):
            saved.append((obj, obj.level))
            obj.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for saved_logger, level in saved:
            saved_logger.setLevel(level)


class DialogGuard:
    """Dismiss dialogs without reading or recording their contents."""

    _JOIN_SECONDS = 2.0
    _SEND_SECONDS = 5.0

    def __init__(self, cdp: Any, session_id: str) -> None:
        self._cdp = cdp
        self._session_id = session_id
        self._tasks: set[asyncio.Task[None]] = set()
        self._on_opening = self._opening

    def start(self) -> None:
        """Subscribe before the caller enables the Page domain."""
        self._cdp.on(
            "Page.javascriptDialogOpening", self._on_opening,
            session_id=self._session_id,
        )

    def _opening(self, _event: dict[str, Any]) -> None:
        task = asyncio.create_task(self._dismiss())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dismiss(self) -> None:
        try:
            await self._cdp.send(
                method="Page.handleJavaScriptDialog",
                params={"accept": False},
                session_id=self._session_id,
                timeout=self._SEND_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("login dialog dismissal failed (details suppressed)")

    async def close(self) -> None:
        """Unsubscribe, settle pending dismissals, then cancel the rest."""
        self._cdp.off("Page.javascriptDialogOpening", self._on_opening)
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=self._JOIN_SECONDS)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
