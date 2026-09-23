"""Answer dialogs on the invocation's session without blocking its CDP reader."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .usage import UsageError

# How long the invocation will spend collecting records once its work is done.
# Answering a dialog can release script that opens another, so the drain has no
# natural end: a page calling alert() from a zero-delay interval supplies a new
# one on every pass. Measured before this bound existed: 191,145 dialogs
# answered in three seconds with the drain still running, and nothing above it
# was bounded either, so `bt eval` hung with no timeout at all. The budget ends
# the collection, not the answering.
DRAIN_BUDGET_SECONDS = 2.0

# After the last answer, give the page one short settle to raise the dialog
# that answer released, so a dialog is not left open by teardown. Only an
# invocation that actually answered something waits: a verb that raised no
# dialog must add no latency at all, which the plain-click case asserts.
SETTLE_SECONDS = 0.05


def validate_policy(value: object) -> str:
    # Typed as object because step_run.run(dialog=...) is a public signature
    # that can reach here with None, which used to raise AttributeError
    # instead of the usage error every other bad value gets.
    if not isinstance(value, str) or (
        value not in {"dismiss", "accept"} and not value.startswith("accept:")
    ):
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
        record = {
            "type": event["type"], "message": event["message"],
            "answer": "accept" if accept else "dismiss", "result": result,
        }
        try:
            await self._cdp.send("Page.handleJavaScriptDialog", params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A dialog that vanished before the answer landed answers
            # "No dialog is showing". Report it as a failed answer rather than
            # raising: this coroutine is gathered with every other record, and
            # an exception here used to discard all of them and escape as a
            # CDPError that neither cli.main handler catches, so a whole Run
            # Document was lost to one late dialog.
            record["error"] = f"{type(exc).__name__}: {exc}"
        return record

    async def document(self) -> dict[str, Any]:
        """Collect what was answered, within a bounded budget."""
        records: list[dict[str, Any]] = []
        deadline = time.monotonic() + DRAIN_BUDGET_SECONDS
        truncated = False
        settled = False
        while True:
            if len(records) < len(self._tasks):
                if time.monotonic() >= deadline:
                    truncated = True
                    break
                pending = self._tasks[len(records):]
                records.extend(await asyncio.gather(*pending))
                settled = False
                continue
            if settled or not self._tasks:
                break
            # Answering one dialog can release script that opens another.
            await asyncio.sleep(SETTLE_SECONDS)
            settled = True

        document: dict[str, Any] = {}
        if records:
            document["dialogs"] = records
        if truncated:
            # Say so rather than report a partial list as the whole truth.
            document["dialogsTruncated"] = True
        return document

    async def close(self) -> None:
        self._cdp.off("Page.javascriptDialogOpening", self._opening)
        # Let an answer already in flight land. Cancelling it would leave the
        # dialog open, which is the state this whole policy exists to prevent.
        inflight = [t for t in self._tasks if not t.done()]
        if inflight:
            await asyncio.wait(inflight, timeout=DRAIN_BUDGET_SECONDS)
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
