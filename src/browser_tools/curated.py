"""Curated CLI actions over isolated core CDP page sessions.

Each invocation resolves its page and owns a CDPRuntime or a one-shot session.
Capture owns its connection until recording and artifact finalization complete.
Native interactions refresh the snapshot before resolving a previously printed UID.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

from . import lifecycle
from .cdp_handler import (
    DEFAULT_IDLE_MS,
    DEFAULT_STABLE_MS,
    DEFAULT_WAIT_TIMEOUT_MS,
    CDPHandler,
    CDPRuntime,
)
from .core import registry as core_registry
from .interstitial import format_interstitials
from .lifecycle import LifecycleError
from .mcp_response import extract_text_items
from .one_shot import cli_cdp_errors, one_shot_page_session, resolve_page_target, target_slot
from .passthrough import UsageError
from .screenshot_utils import (
    SCREENSHOT_BLANK_MAX_RETRIES,
    SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    screenshot_looks_blank,
)
from .tool_registry import TOOLS

#: How long a one-shot handler waits for its CDP connection before giving up.
HANDLER_CONNECT_TIMEOUT_SECONDS = 10.0

#: ``wait-idle`` / ``wait-stable`` defaults (import the handler defaults from
#: ``cdp_handler._handle_wait_idle`` / ``_handle_wait_stable``).


# ---------------------------------------------------------------------------
# Instance / port resolution (same path passthrough and help use)
# ---------------------------------------------------------------------------


def _resolve_port(instance: str | None, registry_path: str | None) -> int:
    """Resolve ``instance`` (omittable) to its registry CDP port.

    An omitted instance resolves via ``lifecycle.resolve_single_instance``; an
    unknown instance becomes a ``LifecycleError`` (CLI exit 1), matching
    ``passthrough.send``.
    """
    if instance is None:
        instance = lifecycle.resolve_single_instance(registry_path=registry_path)
    info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
    return info.port


# ---------------------------------------------------------------------------
# Handler transport: one-shot CDPHandler against a running instance
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _cdp_handler_session(
    port: int,
    target: str | None = None,
    url: str | None = None,
    *,
    stopped: threading.Event | None = None,
) -> Generator[CDPHandler]:
    """Connect one runtime to a resolved target, and close it after the call."""
    spec, by = target_slot(target, url)

    async def resolve() -> str:
        task = asyncio.create_task(resolve_page_target(port, spec, by))
        try:
            deadline = time.monotonic() + HANDLER_CONNECT_TIMEOUT_SECONDS
            while not task.done():
                if stopped is not None and stopped.is_set():
                    raise LifecycleError("Capture cancelled before recording")
                if time.monotonic() >= deadline:
                    raise LifecycleError("CDP target resolution timed out")
                await asyncio.wait({task}, timeout=0.05)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    target_id = asyncio.run(resolve())
    runtime = CDPRuntime(port, target_id)
    thread = threading.Thread(target=runtime.run, name="curated-cdp", daemon=True)
    thread.start()
    try:
        try:
            if stopped is None:
                runtime.wait_ready(HANDLER_CONNECT_TIMEOUT_SECONDS)
            else:
                deadline = time.monotonic() + HANDLER_CONNECT_TIMEOUT_SECONDS
                while True:
                    if stopped.is_set():
                        raise LifecycleError("Capture cancelled before recording")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("CDP connection timed out")
                    try:
                        runtime.wait_ready(min(0.05, remaining))
                        break
                    except TimeoutError:
                        continue
        except TimeoutError as exc:
            raise LifecycleError(str(exc)) from exc
        yield CDPHandler(runtime)
    finally:
        runtime.stop()
        thread.join(timeout=6)


def _envelope_text(resp: dict[str, Any]) -> tuple[str, bool]:
    """Read an MCP envelope into ``(text, is_error)``.

    Uses the canonical ``mcp_response`` reader so every envelope shape the
    handler can return is honored. ``is_error`` reflects the envelope's
    ``isError`` flag, which the CLI front maps to exit code 1.
    """
    texts = extract_text_items(resp)
    text = "\n".join(texts)
    result = resp.get("result")
    is_error = isinstance(result, dict) and result.get("isError") is True
    return text.removeprefix("Error: ") if is_error else text, is_error


def _tool_or_raise(handler: CDPHandler, name: str, arguments: dict[str, Any]) -> str:
    """Run a CDP tool through the handler; raise ``LifecycleError`` on tool error."""
    text, is_error = _envelope_text(handler.call_tool(name, arguments))
    if is_error:
        raise LifecycleError(text)
    return text


def _native_or_raise(handler: CDPHandler, name: str, arguments: dict[str, Any]) -> str:
    """Run a native tool through the handler; raise ``LifecycleError`` on tool error."""
    text, is_error = _envelope_text(handler.call_native(name, arguments))
    if is_error:
        raise LifecycleError(text)
    return text


# ---------------------------------------------------------------------------
# Native snapshot / interaction (over CDPHandler.call_native -> #39/#40 path)
# ---------------------------------------------------------------------------


@cli_cdp_errors
def snapshot(
    *,
    instance: str | None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Return the native UID accessibility tree (frozen ``take_snapshot``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        tree = _native_or_raise(handler, "take_snapshot", {})
    return {"snapshot": tree}


@cli_cdp_errors
def click(
    *,
    instance: str | None,
    uid: str,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Native UID click (frozen ``click``), over the #40 interaction path.

    Takes a fresh snapshot first so the UID resolves against the current,
    identically-ordered tree of the (unchanged) page in this one-shot process.
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        _native_or_raise(handler, "take_snapshot", {})
        text = _native_or_raise(handler, "click", {"uid": uid})
    return {"uid": uid, "result": text}


@cli_cdp_errors
def fill(
    *,
    instance: str | None,
    uid: str,
    text: str,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Native UID fill (frozen ``fill``), over the #40 interaction path."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        _native_or_raise(handler, "take_snapshot", {})
        result = _native_or_raise(handler, "fill", {"uid": uid, "value": text})
    return {"uid": uid, "text": text, "result": result}


# ---------------------------------------------------------------------------
# Semantic waits (over CDPHandler.call_tool -> _handle_wait_idle/_handle_wait_stable)
# ---------------------------------------------------------------------------


@cli_cdp_errors
def wait_idle(
    *,
    instance: str | None,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    idle_ms: int = DEFAULT_IDLE_MS,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Wait for network idle (frozen ``wait_idle``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        text = _tool_or_raise(handler, "wait_idle", {"timeout_ms": timeout_ms, "idle_ms": idle_ms})
    return {"result": text}


@cli_cdp_errors
def wait_stable(
    *,
    instance: str | None,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    stable_ms: int = DEFAULT_STABLE_MS,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Wait for DOM quiescence (frozen ``wait_stable``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        text = _tool_or_raise(
            handler, "wait_stable", {"timeout_ms": timeout_ms, "stable_ms": stable_ms}
        )
    return {"result": text}


# ---------------------------------------------------------------------------
# Interstitial detection (over CDPHandler.run_post_navigation_detection -> interstitial.py)
# ---------------------------------------------------------------------------


@cli_cdp_errors
def detect(
    *,
    instance: str | None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Run interstitial detection against the current page (``inspect_blocked``/``inspect_warn``).

    Drives the exact challenge-response policy in ``interstitial.py`` through
    ``CDPHandler.run_post_navigation_detection`` -- the same detect-and-retry
    used after navigation, surfaced here as a verb.
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        result = handler.runtime.run_post_navigation_detection()
    if result is None:
        raise LifecycleError("interstitial detection unavailable (no CDP session)")
    detections = result.get("detections", [])
    report = format_interstitials(
        detections,
        auto_retried=result.get("auto_retried", False),
        retries_used=result.get("retries_used", 0),
    )
    return {
        "detections": detections,
        "auto_retried": result.get("auto_retried", False),
        "retries_used": result.get("retries_used", 0),
        "report": report,
    }


# ---------------------------------------------------------------------------
# Frames (over CDPHandler.call_tool -> list_frames/select_frame/reset_frame)
# ---------------------------------------------------------------------------


@cli_cdp_errors
def frames_list(
    *,
    instance: str | None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """List the page's frames (frozen ``list_frames``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        text = _tool_or_raise(handler, "list_frames", {})
    return {"frames": text}


@cli_cdp_errors
def frames_select(
    *,
    instance: str | None,
    pattern: str,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Select a frame by URL pattern (frozen ``select_frame``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        text = _tool_or_raise(handler, "select_frame", {"url_pattern": pattern})
    return {"selected": text}


@cli_cdp_errors
def frames_reset(
    *,
    instance: str | None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Clear frame selection back to the top-level page (frozen ``reset_frame``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        text = _tool_or_raise(handler, "reset_frame", {})
    return {"result": text}


# ---------------------------------------------------------------------------
# Storage (over CDPHandler.call_tool -> get_frame_storage)
# ---------------------------------------------------------------------------


@cli_cdp_errors
def storage_get(
    *,
    instance: str | None,
    key: str | None = None,
    reveal_values: bool = False,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Read a frame's storage (frozen ``get_frame_storage``).

    ``get_frame_storage`` reads the *selected* frame, and frame selection does
    not survive between one-shot CLI processes. ``--key`` names the frame to
    read (a URL pattern), selected within this one invocation before the read;
    omitting it surfaces the tool's own "No frame selected" error (exit 1).
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        if key:
            _tool_or_raise(handler, "select_frame", {"url_pattern": key})
        text = _tool_or_raise(handler, "get_frame_storage", {"reveal_values": reveal_values})
    return {"storage": text}


# ---------------------------------------------------------------------------
# Screencast (over CDPHandler.call_tool -> screencast_start/screencast_stop)
# ---------------------------------------------------------------------------


@cli_cdp_errors
def screencast_record(
    *,
    instance: str | None,
    out_dir: str,
    fmt: str = "jpeg",
    max_frames: int = 600,
    duration: float | None = None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Record until signalled or bounded, then write frames before disconnecting."""
    if fmt not in ("jpeg", "png") or max_frames <= 0:
        raise UsageError("Capture requires format jpeg or png and a positive --max-frames")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise UsageError("Capture --duration must be finite and positive")
    output = Path(out_dir).absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise UsageError("Capture --dir must be absent or an empty directory, not a symlink")
    stopped = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        stopped.set()

    prior = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        port = _resolve_port(instance, registry_path)
        with _cdp_handler_session(port, target, url, stopped=stopped) as handler:
            runtime = handler.runtime
            loop = runtime.loop
            assert loop is not None
            start = asyncio.run_coroutine_threadsafe(
                runtime.screencast.start(runtime, {"format": fmt, "max_frames": max_frames}), loop
            )
            deadline = time.monotonic() + HANDLER_CONNECT_TIMEOUT_SECONDS
            while True:
                if stopped.is_set():
                    start.cancel()
                    raise LifecycleError("Capture cancelled before recording")
                if time.monotonic() >= deadline:
                    start.cancel()
                    raise LifecycleError("Capture recorder start timed out")
                try:
                    text, error = _envelope_text(start.result(timeout=0.05))
                    if error:
                        raise LifecycleError(text)
                    break
                except TimeoutError:
                    continue
            print(
                "Recording; send SIGINT or SIGTERM to this process to finish.",
                file=sys.stderr,
                flush=True,
            )
            started = time.monotonic()
            failure: str | None = None
            next_health_check = started
            while not stopped.wait(0.05):
                if runtime.screencast.limit_reached or (
                    duration is not None and time.monotonic() - started >= duration
                ):
                    break
                if time.monotonic() < next_health_check:
                    continue
                next_health_check = time.monotonic() + 1.0
                future = asyncio.run_coroutine_threadsafe(runtime.send("Page.getFrameTree"), loop)
                try:
                    future.result(timeout=5)
                except Exception as exc:
                    future.cancel()
                    failure = f"Capture transport failed: {exc}"
                    break
            # The recorder retains ownership of received frames after transport loss.
            future = asyncio.run_coroutine_threadsafe(
                runtime.screencast.stop(runtime, {"dir": str(output)}), loop
            )
            try:
                text, error = _envelope_text(future.result(timeout=30))
            except TimeoutError as exc:
                future.cancel()
                raise LifecycleError("Capture artifact finalization timed out") from exc
            if error or failure:
                raise LifecycleError(failure or text)
            return {"result": text}
    except (LifecycleError, ConnectionError, TimeoutError) as exc:
        raise LifecycleError(f"{exc}; output: {output}") from exc
    finally:
        for sig, previous in prior.items():
            signal.signal(sig, previous)


screencast_start = screencast_record


def screencast_stop(**kwargs: Any) -> dict[str, Any]:
    """Explain how to stop a foreground Capture without opening a connection."""
    raise UsageError("Send SIGINT or SIGTERM to the owning screencast record process")


# ---------------------------------------------------------------------------
# Screenshot (session transport: Page.captureScreenshot + blank-frame guard)
# ---------------------------------------------------------------------------


async def _capture_screenshot(port: int, target_spec: str | None, target_by: str | None) -> str:
    """Capture a full-page PNG over a one-shot session, guarding blank frames.

    Opens the one-shot page session (the same seam ``passthrough``/``events``
    use) and sends ``Page.captureScreenshot`` over it. Reuses the existing
    blank-frame guard (``screenshot_utils.screenshot_looks_blank`` plus the
    shared retry budget): a near-uniform capture is retried after a short
    delay to allow the compositor to produce a visible frame.
    """
    async with one_shot_page_session(port, target_spec, target_by) as (cdp, session_id):
        data = ""
        for attempt in range(SCREENSHOT_BLANK_MAX_RETRIES + 1):
            result = await cdp.send(
                method="Page.captureScreenshot",
                params={"format": "png"},
                session_id=session_id,
            )
            data = result.get("data", "")
            if not data or not screenshot_looks_blank(data):
                return data
            if attempt < SCREENSHOT_BLANK_MAX_RETRIES:
                await asyncio.sleep(SCREENSHOT_BLANK_RETRY_DELAY_SECONDS)
        return data


@cli_cdp_errors
def screenshot(
    *,
    instance: str | None,
    path: str | None = None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Capture a page screenshot (frozen ``take_screenshot``, CDP-native form).

    ``--path`` writes the PNG to a file; without it the base64 ``data:`` URI is
    returned. ``--target``/``--url`` pick the page target, as on the passthrough
    line. Target-resolution and CDP failures become ``LifecycleError`` (exit 1),
    via ``@cli_cdp_errors``.
    """
    if target is not None and url is not None:
        raise UsageError("cannot specify both --target and --url")
    port = _resolve_port(instance, registry_path)

    spec, target_by = target_slot(target, url)

    data = asyncio.run(_capture_screenshot(port, spec, target_by))

    if not data:
        raise LifecycleError("no screenshot data returned from the browser")

    payload: dict[str, Any] = {}
    if path:
        abs_path = str(Path(path).resolve())
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        Path(abs_path).write_bytes(base64.b64decode(data))
        payload["saved"] = abs_path
    else:
        payload["data"] = f"data:image/png;base64,{data}"
    return payload


@cli_cdp_errors
def tool(
    args: list[str],
    *,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Dispatch one handler tool, validating names before opening CDP."""
    if not args:
        raise UsageError("tool requires NAME")
    instance = None
    remaining = list(args)
    if (
        len(remaining) > 1
        and remaining[0] not in TOOLS
        and (
            remaining[1] in TOOLS
            or any(item.name == remaining[0] for item in lifecycle.read_instances(registry_path))
        )
    ):
        instance = remaining.pop(0)
    name = remaining.pop(0)
    if name not in TOOLS:
        raise UsageError(
            "Unknown handler tool "
            + repr(name)
            + "; available: "
            + ", ".join(name for name, entry in TOOLS.items() if not entry.requires_capture)
        )
    if TOOLS[name].requires_capture:
        raise UsageError("Use screencast record; recording state belongs to one Capture process")
    if len(remaining) > 1:
        raise UsageError("tool accepts one JSON argument object")
    try:
        arguments = json.loads(remaining[0]) if remaining else {}
    except json.JSONDecodeError as exc:
        raise UsageError(f"invalid JSON arguments: {exc}") from exc
    if not isinstance(arguments, dict):
        raise UsageError("tool arguments must be a JSON object")
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port, target, url) as handler:
        result = {"result": _tool_or_raise(handler, name, arguments)}
        if TOOLS[name].navigation:
            handler.runtime.run_post_navigation_detection()
        return result
