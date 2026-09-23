"""Answer dialogs on the invocation's session without blocking its CDP reader."""

from __future__ import annotations

import asyncio
from typing import Any

from .usage import UsageError


def validate_policy(value: str) -> str:
    if value not in {"dismiss", "accept"} and not value.startswith("accept:"):
        raise UsageError("--dialog must be dismiss, accept, or accept:TEXT")
    return value


class DialogPolicy:
    def __init__(self, value: str) -> None:
        self.value = validate_policy(value)
        self._tasks: list[asyncio.Task[dict[str, Any]]] = []
        self._cdp: Any = None
        self._opening = self._opened

    def start(self, cdp: Any) -> None:
        self._cdp = cdp
        cdp.on("Page.javascriptDialogOpening", self._opening)

    def _opened(self, event: dict[str, Any]) -> None:
        # The reader must return to receiving before a command can get its reply.
        self._tasks.append(asyncio.create_task(self._answer(event)))

    async def _answer(self, event: dict[str, Any]) -> dict[str, Any]:
        accept = self.value != "dismiss"
        params: dict[str, Any] = {"accept": accept}
        result: Any = None
        if event["type"] == "prompt" and accept:
            result = self.value.partition(":")[2] if ":" in self.value else event.get("defaultPrompt", "")
            params["promptText"] = result
        elif event["type"] in {"confirm", "beforeunload"}:
            result = accept
        await self._cdp.send("Page.handleJavaScriptDialog", params)
        return {
            "type": event["type"], "message": event["message"],
            "answer": "accept" if accept else "dismiss", "result": result,
        }

    async def document(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        # Answering one dialog can release script that immediately opens another.
        while len(records) < len(self._tasks):
            records.extend(await asyncio.gather(*self._tasks[len(records):]))
        return {"dialogs": records} if records else {}

    async def close(self) -> None:
        self._cdp.off("Page.javascriptDialogOpening", self._opening)
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
