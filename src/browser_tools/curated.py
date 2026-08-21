"""Curated tool verbs for the merged CLI front (RFC-01 #50).

New layer-4 code, alongside ``cli.py``, ``lifecycle.py``, ``passthrough.py``,
and ``events.py``. It fronts the curated tools -- the high-level actions the
frozen MCP surface exposes -- as first-class ``browser-tools``/``bt`` verbs, so
the CLI-first surface matches RFC-01's normative "CLI surface" section.

Every verb here dispatches to the *same* implementation the matching MCP tool
uses. This module builds no second engine (RFC-01 invariant: "curated tools are
CDP consumers over the same client the passthrough uses"). It reaches those
implementations through two one-shot transports, chosen by what the tool needs:

- **Handler transport** (:func:`_cdp_handler_session`). Spins up one
  :class:`~browser_tools.cdp_handler.CDPHandler` against the running instance,
  drives it through its public ``call_tool`` / ``call_native`` /
  ``run_post_navigation_detection`` surface -- the exact methods the MCP daemon
  dispatches to -- then tears it down. Used by the tools whose implementation
  lives on ``CDPHandler`` / ``CDPRuntime`` and needs the frame manager, native
  snapshot reader, screencast recorder, or interstitial policy the runtime owns:
  ``snapshot``, ``click``, ``fill``, ``wait-idle``, ``wait-stable``, ``detect``,
  ``frames``, ``storage``, ``screencast``.

- **Session transport** (:func:`_capture_screenshot`). Opens one browser-level
  ``core.cdp_client.CDPClient``, resolves a page target, and sends over the
  session -- the same one-shot ``send`` path ``passthrough``/``events`` use.
  Used only by ``screenshot``, whose full-page capture has no Python-native
  ``CDPHandler`` tool (the frozen ``take_screenshot`` forwards to the Node
  broker); the CDP-native form is ``Page.captureScreenshot`` plus the existing
  blank-frame guard (``screenshot_utils.screenshot_looks_blank``).

Instance resolution matches the other verbs: an omitted ``INSTANCE`` resolves
via :func:`lifecycle.resolve_single_instance` (fails naming the candidates
unless exactly one instance is registered), and the port is read from the
registry exactly as ``passthrough``/``help`` do. Operational failures raise
:class:`~browser_tools.lifecycle.LifecycleError` (CLI exit 1); malformed
invocations raise :class:`~browser_tools.passthrough.UsageError` (CLI exit 2).

Single-invocation snapshots
---------------------------
The native UID scheme is deterministic: the same accessibility tree yields the
same UID ordinals on every read, differing only by a per-reader generation
prefix (see ``native_snapshot``). A one-shot ``click``/``fill`` therefore takes
a fresh ``take_snapshot`` first, over the same handler, so the UID a prior
``snapshot`` verb printed resolves against an identically-ordered tree of the
unchanged page before the interaction dispatches.

Cross-process state (frame selection carried from ``frames select`` into a
later ``storage get``, and ``screencast start`` buffering read by a later
``screencast stop``) does not survive between independent CLI processes, which
each own a fresh handler. ``storage get --key`` selects its frame within the
one invocation; ``screencast`` capture across two processes is a known limit of
the one-shot CLI and is exercised only against the persistent MCP front.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from . import lifecycle
from .cdp_handler import CDPHandler
from .core import registry as core_registry
from .core.attach import AmbiguousTargetError, TargetNotFoundError, resolve_target
from .core.cdp_client import CDPClient, get_ws_url
from .core.errors import CDPError
from .core.registry import InstanceNotFoundError
from .interstitial import format_interstitials
from .lifecycle import LifecycleError
from .mcp_response import extract_text_items
from .passthrough import UsageError
from .screenshot_utils import (
    SCREENSHOT_BLANK_MAX_RETRIES,
    SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    screenshot_looks_blank,
)

#: How long a one-shot handler waits for its CDP connection before giving up.
HANDLER_CONNECT_TIMEOUT_SECONDS = 10.0

#: ``wait-idle`` / ``wait-stable`` defaults (mirror the MCP tool defaults in
#: ``cdp_handler._handle_wait_idle`` / ``_handle_wait_stable``).
DEFAULT_WAIT_TIMEOUT_MS = 5000
DEFAULT_IDLE_MS = 500
DEFAULT_STABLE_MS = 300


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
    try:
        info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc
    return info.port


# ---------------------------------------------------------------------------
# Handler transport: one-shot CDPHandler against a running instance
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _cdp_handler_session(port: int) -> Iterator[CDPHandler]:
    """Yield a connected one-shot :class:`CDPHandler`, then tear it down.

    Builds the handler against ``http://127.0.0.1:{port}``, runs its event loop
    on a background thread (the same runtime the MCP daemon threads), waits for
    the CDP connection to come up, and always stops it afterward. A connection
    that never comes up within :data:`HANDLER_CONNECT_TIMEOUT_SECONDS` is a
    ``LifecycleError`` (CLI exit 1).
    """
    handler = CDPHandler(f"http://127.0.0.1:{port}", mode="full")
    thread = threading.Thread(target=handler.run, name="curated-cdp", daemon=True)
    thread.start()
    deadline = time.monotonic() + HANDLER_CONNECT_TIMEOUT_SECONDS
    connected = False
    while time.monotonic() < deadline:
        if handler.available:
            connected = True
            break
        time.sleep(0.02)
    if not connected:
        handler.stop()
        thread.join(timeout=2.0)
        raise LifecycleError(
            f"could not open a CDP session on the instance at port {port}"
        )
    try:
        yield handler
    finally:
        handler.stop()
        thread.join(timeout=2.0)


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
    return text, is_error


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


def snapshot(*, instance: str | None, registry_path: str | None = None) -> dict[str, Any]:
    """Return the native UID accessibility tree (frozen ``take_snapshot``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        tree = _native_or_raise(handler, "take_snapshot", {})
    return {"snapshot": tree}


def click(*, instance: str | None, uid: str, registry_path: str | None = None) -> dict[str, Any]:
    """Native UID click (frozen ``click``), over the #40 interaction path.

    Takes a fresh snapshot first so the UID resolves against the current,
    identically-ordered tree of the (unchanged) page in this one-shot process.
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        _native_or_raise(handler, "take_snapshot", {})
        text = _native_or_raise(handler, "click", {"uid": uid})
    return {"uid": uid, "result": text}


def fill(
    *, instance: str | None, uid: str, text: str, registry_path: str | None = None
) -> dict[str, Any]:
    """Native UID fill (frozen ``fill``), over the #40 interaction path."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        _native_or_raise(handler, "take_snapshot", {})
        result = _native_or_raise(handler, "fill", {"uid": uid, "value": text})
    return {"uid": uid, "text": text, "result": result}


# ---------------------------------------------------------------------------
# Semantic waits (over CDPHandler.call_tool -> _handle_wait_idle/_handle_wait_stable)
# ---------------------------------------------------------------------------


def wait_idle(
    *,
    instance: str | None,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    idle_ms: int = DEFAULT_IDLE_MS,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Wait for network idle (frozen ``wait_idle``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(
            handler, "wait_idle", {"timeout_ms": timeout_ms, "idle_ms": idle_ms}
        )
    return {"result": text}


def wait_stable(
    *,
    instance: str | None,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    stable_ms: int = DEFAULT_STABLE_MS,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Wait for DOM quiescence (frozen ``wait_stable``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(
            handler, "wait_stable", {"timeout_ms": timeout_ms, "stable_ms": stable_ms}
        )
    return {"result": text}


# ---------------------------------------------------------------------------
# Interstitial detection (over CDPHandler.run_post_navigation_detection -> interstitial.py)
# ---------------------------------------------------------------------------


def detect(*, instance: str | None, registry_path: str | None = None) -> dict[str, Any]:
    """Run interstitial detection against the current page (``inspect_blocked``/``inspect_warn``).

    Drives the exact challenge-response policy in ``interstitial.py`` through
    ``CDPHandler.run_post_navigation_detection`` -- the same detect-and-retry
    the daemon runs automatically post-navigation, surfaced here as a verb.
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        result = handler.run_post_navigation_detection()
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


def frames_list(*, instance: str | None, registry_path: str | None = None) -> dict[str, Any]:
    """List the page's frames (frozen ``list_frames``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(handler, "list_frames", {})
    return {"frames": text}


def frames_select(
    *, instance: str | None, pattern: str, registry_path: str | None = None
) -> dict[str, Any]:
    """Select a frame by URL pattern (frozen ``select_frame``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(handler, "select_frame", {"url_pattern": pattern})
    return {"selected": text}


def frames_reset(*, instance: str | None, registry_path: str | None = None) -> dict[str, Any]:
    """Clear frame selection back to the top-level page (frozen ``reset_frame``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(handler, "reset_frame", {})
    return {"result": text}


# ---------------------------------------------------------------------------
# Storage (over CDPHandler.call_tool -> get_frame_storage)
# ---------------------------------------------------------------------------


def storage_get(
    *, instance: str | None, key: str | None = None, registry_path: str | None = None
) -> dict[str, Any]:
    """Read a frame's storage (frozen ``get_frame_storage``).

    ``get_frame_storage`` reads the *selected* frame, and frame selection does
    not survive between one-shot CLI processes. ``--key`` names the frame to
    read (a URL pattern), selected within this one invocation before the read;
    omitting it surfaces the tool's own "No frame selected" error (exit 1).
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        if key:
            _tool_or_raise(handler, "select_frame", {"url_pattern": key})
        text = _tool_or_raise(handler, "get_frame_storage", {})
    return {"storage": text}


# ---------------------------------------------------------------------------
# Screencast (over CDPHandler.call_tool -> screencast_start/screencast_stop)
# ---------------------------------------------------------------------------


def screencast_start(
    *,
    instance: str | None,
    fmt: str = "jpeg",
    max_frames: int = 600,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Begin screencast capture (frozen ``screencast_start``).

    Capture is stateful in the recorder the handler owns, so a one-shot
    ``screencast start`` followed by an independent ``screencast stop`` process
    cannot share buffered frames. The verb dispatches correctly; end-to-end
    capture is meaningful only against the persistent MCP front.
    """
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(
            handler, "screencast_start", {"format": fmt, "max_frames": max_frames}
        )
    return {"result": text}


def screencast_stop(
    *, instance: str | None, out_dir: str, registry_path: str | None = None
) -> dict[str, Any]:
    """Stop screencast capture and write frames (frozen ``screencast_stop``)."""
    port = _resolve_port(instance, registry_path)
    with _cdp_handler_session(port) as handler:
        text = _tool_or_raise(handler, "screencast_stop", {"dir": out_dir})
    return {"result": text}


# ---------------------------------------------------------------------------
# Screenshot (session transport: Page.captureScreenshot + blank-frame guard)
# ---------------------------------------------------------------------------


async def _capture_screenshot(
    port: int, target_spec: str | None, target_by: str | None
) -> str:
    """Capture a full-page PNG over a one-shot session, guarding blank frames.

    Opens the browser-level connection, resolves a page target, attaches an
    isolated ``Target`` session (the same plumbing ``passthrough`` uses), and
    sends ``Page.captureScreenshot``. Reuses the existing blank-frame guard
    (``screenshot_utils.screenshot_looks_blank`` plus the shared retry budget):
    a near-uniform capture is retried after a short delay, matching the daemon's
    ``take_screenshot`` post-capture check.
    """
    browser_ws_url = get_ws_url(port=port, target_type="browser")
    async with CDPClient(ws_url=browser_ws_url) as cdp:
        targets_result = await cdp.send(method="Target.getTargets")
        page_targets = sorted(
            (t for t in targets_result.get("targetInfos", []) if t.get("type") == "page"),
            key=lambda t: t.get("targetId", ""),
        )
        if not page_targets:
            raise LifecycleError("No page targets in browser")

        target_id = resolve_target(
            page_targets=page_targets, target_spec=target_spec, target_by=target_by
        )
        session_result = await cdp.send(
            method="Target.attachToTarget",
            params={"targetId": target_id, "flatten": True},
        )
        session_id = session_result["sessionId"]
        try:
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
        finally:
            with contextlib.suppress(Exception):
                await cdp.send(
                    method="Target.detachFromTarget",
                    params={"sessionId": session_id},
                )


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
    line. Target-resolution and CDP failures become ``LifecycleError`` (exit 1).
    """
    if target is not None and url is not None:
        raise UsageError("cannot specify both --target and --url")
    port = _resolve_port(instance, registry_path)

    spec: str | None = None
    target_by: str | None = None
    if target is not None:
        spec = target
        target_by = "index" if target.isdigit() else "id"
    elif url is not None:
        spec = url
        target_by = "url"

    try:
        data = asyncio.run(_capture_screenshot(port, spec, target_by))
    except (AmbiguousTargetError, TargetNotFoundError) as exc:
        raise LifecycleError(str(exc)) from exc
    except CDPError as exc:
        raise LifecycleError(f"CDP error {exc.code}: {exc.message}") from exc
    except ConnectionError as exc:
        raise LifecycleError(str(exc)) from exc

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
