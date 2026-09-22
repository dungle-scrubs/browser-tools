"""CDP operations for RFC-05's six verbs, on the handler's existing session."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from .lifecycle import LifecycleError
from .one_shot import domains_enabled
from .usage import UsageError

KEYS: dict[str, tuple[str, int, str]] = {
    "Enter": ("Enter", 13, "\r"),
    "Tab": ("Tab", 9, ""),
    "Escape": ("Escape", 27, ""),
    "Backspace": ("Backspace", 8, ""),
    "Delete": ("Delete", 46, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""),
    "ArrowUp": ("ArrowUp", 38, ""),
    "ArrowRight": ("ArrowRight", 39, ""),
    "ArrowDown": ("ArrowDown", 40, ""),
    "Home": ("Home", 36, ""),
    "End": ("End", 35, ""),
    "PageUp": ("PageUp", 33, ""),
    "PageDown": ("PageDown", 34, ""),
    **{f"F{i}": (f"F{i}", 111 + i, "") for i in range(1, 13)},
}
MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_PUNCTUATION = {
    " ": ("Space", 32),
    ";:": ("Semicolon", 186),
    "=+": ("Equal", 187),
    ",<": ("Comma", 188),
    "-_": ("Minus", 189),
    ".>": ("Period", 190),
    "/?": ("Slash", 191),
    "`~": ("Backquote", 192),
    "[{": ("BracketLeft", 219),
    "\\|": ("Backslash", 220),
    "]}": ("BracketRight", 221),
    "'\"": ("Quote", 222),
}
DEFAULT_NETWORK_DURATION = 2.0
MAX_INLINE_BODY_BYTES = 1024 * 1024


def key_event(key: str, modifiers: str | None) -> tuple[dict[str, Any], list[str]]:
    """Validate names before connecting and return a complete keyDown payload."""
    # Strip each name: a shell-quoted "Control, Shift" is one argument with a
    # space in it, and splitting on the comma alone made the second name unknown.
    names = (
        [n for n in dict.fromkeys(part.strip() for part in modifiers.split(",")) if n]
        if modifiers is not None
        else []
    )
    if any(name not in MODIFIERS for name in names):
        raise UsageError(f"unknown modifier; valid names: {', '.join(MODIFIERS)}")
    if key in KEYS:
        code, virtual, text = KEYS[key]
    elif len(key) == 1 and key.isprintable():
        text = key
        if key.isascii() and key.isalpha():
            code, virtual = f"Key{key.upper()}", ord(key.upper())
        elif key in "0123456789":
            code, virtual = f"Digit{key}", ord(key)
        elif key in ")!@#$%^&*(":
            digit = ")!@#$%^&*(".index(key)
            code, virtual = f"Digit{digit}", ord(str(digit))
        else:
            code, virtual = next(
                (value for chars, value in _PUNCTUATION.items() if key in chars),
                ("", 0),
            )
    else:
        raise UsageError(
            f"unknown key {key!r}; use a single printable character or: {', '.join(KEYS)}"
        )
    mask = sum(MODIFIERS[name] for name in names)
    return {
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": virtual,
        "nativeVirtualKeyCode": virtual,
        "text": text if not mask & 7 else "",
        "modifiers": mask,
    }, names


def evaluated(result: dict[str, Any]) -> dict[str, Any]:
    """A JavaScript exception is an operational failure, even when CDP succeeded."""
    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        exception = details.get("exception", {})
        raise LifecycleError(
            str(
                exception.get("description")
                or exception.get("value")
                or details.get("text")
                or json.dumps(details)
            )
        )
    return result.get("result", {})


async def evaluate(
    cdp: Any,
    source: str,
    await_promise: bool,
    context: int | None,
    timeout_ms: int = 30000,
    page_cdp: Any = None,
) -> dict[str, Any]:
    """Compile without execution to distinguish expressions from statement bodies.

    Direct eval inside the IIFE uses JavaScript's completion value. Splitting
    on semicolons would corrupt strings, comments, templates and nested blocks.
    A thrown SyntaxError must never cause the user's code to execute twice.
    """
    expression = f"({source}\n)"
    compiled = await cdp.send(
        "Runtime.compileScript",
        {
            "expression": expression,
            "sourceURL": "",
            "persistScript": False,
            **({"executionContextId": context} if context is not None else {}),
        },
    )
    if "exceptionDetails" in compiled:
        script = await cdp.send(
            "Runtime.compileScript",
            {
                "expression": source,
                "sourceURL": "",
                "persistScript": False,
                **({"executionContextId": context} if context is not None else {}),
            },
        )
        expression = (
            f"(() => {{ {source}\n }})()"
            if "exceptionDetails" in script
            else f"(() => {{ return eval({json.dumps(source)}); }})()"
        )
    result = evaluated(
        await cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "timeout": max(1, timeout_ms),
                **({"contextId": context} if context is not None else {}),
            },
        )
    )
    page = cdp if page_cdp is None else page_cdp
    info = (await page.send("Target.getTargetInfo"))["targetInfo"]
    return {
        "value": result.get("value", result.get("unserializableValue")),
        "type": result.get("type", "undefined"),
        "url": info["url"],
        "targetId": info["targetId"],
    }


def wait_text_expression(substring: str, timeout_ms: int) -> str:
    """Check and install the observer in one JS task, with no subscription gap."""
    return """new Promise((resolve) => {
      const start = performance.now();
      const substring = SUBSTRING;
      const present = () => (document.body?.innerText ?? '').includes(substring);
      const result = found => ({found, substring, waitedMs: Math.round(performance.now()-start)});
      if (present()) { resolve(result(true)); return; }
      let timer;
      const finish = found => { observer.disconnect(); clearTimeout(timer); resolve(result(found)); };
      const observer = new MutationObserver(() => { if (present()) finish(true); });
      observer.observe(document, {subtree:true, childList:true, characterData:true, attributes:true});
      timer = setTimeout(() => finish(present()), TIMEOUT);
      if (present()) finish(true);
    })""".replace("TIMEOUT", str(timeout_ms)).replace("SUBSTRING", json.dumps(substring))


async def wait_text(
    cdp: Any, substring: str, timeout_ms: int, context: int | None
) -> dict[str, Any]:
    result = evaluated(
        await cdp.send(
            "Runtime.evaluate",
            {
                "expression": wait_text_expression(substring, timeout_ms),
                "returnByValue": True,
                "awaitPromise": True,
                **({"contextId": context} if context is not None else {}),
            },
            timeout=timeout_ms / 1000 + 5,
        )
    )
    value: dict[str, Any] = result["value"]
    if not value["found"]:
        raise LifecycleError(f"wait-text timed out after {timeout_ms} ms waiting for {substring!r}")
    return value


async def focus_selected(cdp: Any, context: int | None) -> None:
    if context is None:
        return
    result = evaluated(
        await cdp.send(
            "Runtime.evaluate",
            {
                "expression": "document.activeElement",
                "contextId": context,
            },
        )
    )
    object_id = result.get("objectId")
    if not object_id:
        raise LifecycleError("selected frame has no active element")
    try:
        await cdp.send("DOM.focus", {"objectId": object_id})
    finally:
        await cdp.send("Runtime.releaseObject", {"objectId": object_id})


def response_document(
    event: dict[str, Any], payload: dict[str, Any], matched: int, response_file: str | None
) -> dict[str, Any]:
    response = event["response"]
    body: str = payload["body"]
    encoded = bool(payload.get("base64Encoded", False))
    data = base64.b64decode(body, validate=True) if encoded else body.encode("utf-8")
    result: dict[str, Any] = {
        "requestId": event["requestId"],
        "url": response["url"],
        "status": response["status"],
        "mimeType": response["mimeType"],
        "bytes": len(data),
        "matched": matched,
        "base64Encoded": encoded,
    }
    if response_file is not None:
        path = Path(response_file).resolve()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise LifecycleError(f"cannot write response body to {path}: {exc}") from exc
        result["responseFile"] = str(path)
    elif encoded:
        result["bodyOmitted"] = "base64-encoded body; use --response-file PATH"
    elif len(data) > MAX_INLINE_BODY_BYTES:
        result["bodyOmitted"] = "body exceeds 1 MiB; use --response-file PATH"
    else:
        result["body"] = body
    return result


class ResponseBuffer:
    """Response metadata and completion state for one attached session.

    Bodies stay in Chrome until requested. This buffer preserves arrival order;
    the last matching response observed at lookup wins, and is never consumed.
    """

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.finished: set[str] = set()
        self.failed: dict[str, str] = {}
        self.changed = asyncio.Event()

    def received(self, event: dict[str, Any]) -> None:
        self.responses.append(event)
        self.changed.set()

    def completed(self, event: dict[str, Any]) -> None:
        self.finished.add(event["requestId"])
        self.changed.set()

    def broken(self, event: dict[str, Any]) -> None:
        self.failed[event["requestId"]] = event.get("errorText", "loading failed")
        self.changed.set()

    @contextlib.asynccontextmanager
    async def capture(
        self, cdp: Any, keep: frozenset[str] = frozenset()
    ) -> AsyncGenerator[ResponseBuffer]:
        listeners = {
            "Network.responseReceived": self.received,
            "Network.loadingFinished": self.completed,
            "Network.loadingFailed": self.broken,
        }
        for event, callback in listeners.items():
            cdp.on(event, callback)
        try:
            async with domains_enabled(cdp.raw, cdp.session_id, ("Network",), keep):
                yield self
        finally:
            for event, callback in listeners.items():
                cdp.off(event, callback)

    async def match(
        self, url: str | None, request_id: str | None, frame_id: str | None,
        duration: float,
    ) -> tuple[dict[str, Any], int]:
        deadline = asyncio.get_running_loop().time() + duration
        while True:
            # No await between clearing, inspecting, and subscribing to changes.
            self.changed.clear()
            matches = [event for event in self.responses
                       if (frame_id is None or event.get("frameId") == frame_id)
                       and ((url is not None and url in event["response"]["url"])
                            or (request_id is not None and request_id == event["requestId"]))]
            if matches:
                last = matches[-1]
                identity = last["requestId"]
                if identity in self.failed:
                    raise LifecycleError(f"request {identity} failed: {self.failed[identity]}")
                if identity in self.finished:
                    return last, len(matches)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if matches:
                    raise LifecycleError(
                        f"response {matches[-1]['requestId']} did not finish in "
                        f"network-get's {duration:g}-second window"
                    )
                raise LifecycleError(
                    f"no matching response in network-get's {duration:g}-second window; "
                    "trigger traffic in an earlier step or use standalone --reload; "
                    "request ids from another invocation cannot retrieve bodies"
                )
            # Recheck state at the deadline, including a just-arrived event.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), remaining)


async def network_get(
    cdp: Any,
    url: str | None,
    request_id: str | None,
    response_file: str | None,
    frame_id: str | None,
    frame_url: str | None,
    keep: frozenset[str],
    duration: float = DEFAULT_NETWORK_DURATION,
    reload: bool = False,
    buffer: ResponseBuffer | None = None,
) -> dict[str, Any]:
    """Read the run's responses, or observe an independent bounded window."""
    if buffer is None:
        async with ResponseBuffer().capture(cdp, keep) as captured:
            if reload:
                if frame_id is None:
                    await cdp.send("Page.reload")
                else:
                    navigation = await cdp.send("Page.navigate", {"url": frame_url, "frameId": frame_id})
                    if navigation.get("errorText"):
                        raise LifecycleError(f"cannot reload selected frame: {navigation['errorText']}")
            return await network_get(cdp, url, request_id, response_file, frame_id,
                                     frame_url, keep, duration, buffer=captured)
    last, matched = await buffer.match(url, request_id, frame_id, duration)
    body = await cdp.send("Network.getResponseBody", {"requestId": last["requestId"]})
    return response_document(last, body, matched, response_file)
