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

Cross-process state does not survive between independent CLI processes, which
each own a fresh handler. Both verbs that needed it do their whole job inside
one invocation instead: ``storage get --key`` selects its frame before reading,
and ``screencast --dir DIR`` starts the capture, waits out its bound, and
writes the frames over one handler.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

from . import endpoint as browser_endpoint
from . import lifecycle
from .cdp_handler import CDPHandler
from .interstitial import INTERSTITIAL_RETRY_DELAY_SECONDS, format_interstitials
from .lifecycle import LifecycleError
from .mcp_response import extract_text_items
from .one_shot import cli_cdp_errors, one_shot_page_session
from .passthrough import HIDDEN_TAB_MESSAGE, UsageError
from .screenshot_utils import (
    SCREENSHOT_BLANK_MAX_RETRIES,
    SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    screenshot_looks_blank,
)

#: How long a one-shot handler waits for its CDP connection before giving up.
HANDLER_CONNECT_TIMEOUT_SECONDS = 10.0

#: ``screencast`` defaults. One bounded capture per invocation (RFC-01 v6):
#: it ends at whichever comes first, the duration or the frame cap.
DEFAULT_SCREENCAST_DURATION_SECONDS = 5.0
DEFAULT_SCREENCAST_MAX_FRAMES = 600

#: How often the capture loop checks whether the frame buffer has filled.
SCREENCAST_POLL_SECONDS = 0.05

#: ``wait-idle`` / ``wait-stable`` defaults (mirror the MCP tool defaults in
#: ``cdp_handler._handle_wait_idle`` / ``_handle_wait_stable``).
DEFAULT_WAIT_TIMEOUT_MS = 5000
DEFAULT_IDLE_MS = 500
DEFAULT_STABLE_MS = 300


# ---------------------------------------------------------------------------
# Instance / port resolution (same path passthrough and help use)
# ---------------------------------------------------------------------------


def _resolve_port(
    instance: str | None, registry_path: str | None, endpoint: str | None = None
) -> int:
    """Resolve the CDP port for this invocation.

    ``endpoint`` drives an external browser and skips the registry entirely;
    otherwise ``instance`` (omittable) resolves to its registry port. One
    resolver for both, shared with ``passthrough``/``events``/``list_verbs``:
    ``lifecycle.resolve_cdp_port``.
    """
    return lifecycle.resolve_cdp_port(instance, registry_path, endpoint)


# ---------------------------------------------------------------------------
# Handler transport: one-shot CDPHandler against a running instance
# ---------------------------------------------------------------------------


def target_selector(target: str | None, url: str | None) -> tuple[str | None, str | None]:
    """``(spec, by)`` for :class:`CDPHandler`, from ``--target`` / ``--url``.

    One derivation, because two would drift: `--target 1` is an index and
    `--target A1B2` is an id, and a caller that got that test wrong would
    attach to the wrong page rather than fail. Raises ``UsageError`` (exit 2)
    when both name the page, since they fill the same slot.
    """
    if target is not None and url is not None:
        raise UsageError("cannot specify both --target and --url")
    if target is not None:
        return target, ("index" if target.isdigit() else "id")
    if url is not None:
        return url, "url"
    return None, None


@contextlib.contextmanager
def capture_session(
    instance: str | None,
    target: str | None,
    registry_path: str | None,
    endpoint: str | None,
) -> Generator[CDPHandler]:
    """A plain session for a Bounded Capture that drives nothing itself.

    ``run_session`` also calls ``start_run_network()``, which enables the
    Network domain and retains response metadata for every request. A Step Run
    needs that, because a step may call ``network-get``. A ``trace --duration``
    or a ``heap`` has no steps, so it is extra browser state and extra CDP
    traffic switched on inside a measurement window, for a buffer nothing can
    read. ``screencast``, the other Bounded Capture, has always used the plain
    session.
    """
    with _handler_for(None, instance, target, registry_path, endpoint) as handler:
        yield handler


@contextlib.contextmanager
def run_session(
    instance: str | None,
    target: str | None,
    url: str | None,
    registry_path: str | None,
    endpoint: str | None,
    all_frames: bool = False,
    dialog: str | None = "dismiss",
) -> Generator[CDPHandler]:
    """The one session a Step Run holds for all of its steps.

    A run resolves the instance and the page once, before step 1, so this is
    the only place either is read. Every step then runs over what this
    yields; nothing below opens a second connection.
    """
    spec, by = target_selector(target, url)
    port = _resolve_port(instance, registry_path, endpoint)
    with _cdp_handler_session(
        port, spec, by, external=endpoint is not None, all_frames=all_frames, dialog=dialog
    ) as handler:
        handler.start_run_network()
        yield handler



@contextlib.contextmanager
def _handler_for(
    handler: CDPHandler | None,
    instance: str | None,
    target: str | None,
    registry_path: str | None,
    endpoint: str | None,
    all_frames: bool = False,
    url: str | None = None,
    dialog: str | None = None,
) -> Generator[CDPHandler]:
    """Yield the session this verb runs on, opening one only if it must.

    Every curated verb opens its own connection when called alone. Inside a
    Step Run the session is opened once and shared, so the run passes the
    handler in and this yields it untouched. Ownership decides teardown: a
    handler passed in is never closed here, including when the body raises,
    because the next step still needs it.
    """
    spec, by = target_selector(target, url)
    if handler is not None:
        yield handler
        return
    port = _resolve_port(instance, registry_path, endpoint)
    with _cdp_handler_session(
        port, spec, external=endpoint is not None, all_frames=all_frames,
        **({"target_by": by} if url is not None else {}),
        **({"dialog": dialog} if dialog is not None else {}),
    ) as opened:
        yield opened


@contextlib.contextmanager
def _cdp_handler_session(
    port: int,
    target_spec: str | None = None,
    target_by: str | None = None,
    *,
    external: bool = False,
    all_frames: bool = False,
    dialog: str | None = None,
) -> Generator[CDPHandler]:
    """Yield a connected one-shot :class:`CDPHandler`, then tear it down.

    Builds the handler against ``http://127.0.0.1:{port}``, runs its event loop
    on a background thread (the same runtime the MCP daemon threads), waits for
    the CDP connection to come up, and always stops it afterward. A connection
    that never comes up within :data:`HANDLER_CONNECT_TIMEOUT_SECONDS` is a
    ``LifecycleError`` (CLI exit 1).

    ``target_spec`` names the page, read the same way ``passthrough.send``
    reads ``--target``: a 1-based index into the page targets sorted by target
    ID, or a target ID prefix. None takes the first page in that order.

    ``external`` says the port came from ``--endpoint``. A failed connection
    then also reports who holds the port and what profile directory they hold:
    there is no registry entry to compare against and no ``bt status`` row to
    look the browser up in (#97).
    """
    handler = CDPHandler(
        f"http://127.0.0.1:{port}",
        mode="full",
        target_spec=target_spec,
        target_by=target_by,
        all_frames=all_frames,
    )
    if dialog is not None:
        handler.configure_dialog_policy(dialog)
    thread = threading.Thread(target=handler.run, name="curated-cdp", daemon=True)
    thread.start()
    deadline = time.monotonic() + HANDLER_CONNECT_TIMEOUT_SECONDS
    connected = False
    reason: str | None = None
    while time.monotonic() < deadline:
        if handler.available:
            connected = True
            break
        # The runtime knows why it failed. Waiting out the whole deadline to
        # then report a generic message throws that away and costs the user
        # ten seconds to say less.
        reason = handler.connect_error
        if reason is not None:
            break
        time.sleep(0.02)
    if not connected:
        handler.stop()
        thread.join(timeout=2.0)
        message = f"could not open a CDP session on the instance at port {port}"
        if reason:
            message = f"{message}: {reason}"
        if external:
            message = (
                f"--endpoint on port {port} did not answer. "
                f"Start the browser with --remote-debugging-port={port}.\n"
                f"{browser_endpoint.describe_endpoint(port)}"
            )
        raise LifecycleError(message)
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


#: The envelope's own error prefix, which the CLI front replaces with its own.
_ENVELOPE_ERROR_PREFIX = "Error: "


def _reason(text: str) -> str:
    """The envelope's error text, without the prefix the CLI adds back.

    `mcp_response.make_error` prefixes `Error: ` for a daemon socket that no
    longer exists (RFC-01 deleted the MCP front), and `cli.py` prefixes
    `error: ` on the way to stderr. Together they printed
    `error: Error: E002: ...`. Only one prefix belongs to the CLI, and it is
    the CLI's.

    Stripped here rather than at the print, because this is where envelope
    text becomes an exception message and the message is what every caller
    then reads. A message that carries no prefix is returned unchanged, so
    the diagnostics that write their own wording through `error_response`
    are untouched.
    """
    return text[len(_ENVELOPE_ERROR_PREFIX):] if text.startswith(_ENVELOPE_ERROR_PREFIX) else text


def _tool_or_raise(handler: CDPHandler, name: str, arguments: dict[str, Any]) -> str:
    """Run a CDP tool through the handler; raise ``LifecycleError`` on tool error."""
    text, is_error = _envelope_text(handler.call_tool(name, arguments))
    if is_error:
        raise LifecycleError(_reason(text))
    return text


def _native_or_raise(handler: CDPHandler, name: str, arguments: dict[str, Any]) -> str:
    """Run a native tool through the handler; raise ``LifecycleError`` on tool error."""
    text, is_error = _envelope_text(handler.call_native(name, arguments))
    if is_error:
        raise LifecycleError(_reason(text))
    return text


# ---------------------------------------------------------------------------
# Native snapshot / interaction (over CDPHandler.call_native -> #39/#40 path)
# ---------------------------------------------------------------------------


def _refuse_input_to_hidden_tab(handler: CDPHandler) -> None:
    """Raise ``LifecycleError`` when the attached page is a background tab.

    Chrome drops input sent to a background tab without an error, so a verb
    that dispatched anyway would print a success result for an interaction
    that never happened. The raw passthrough already refuses this
    (``passthrough._refuse_input_to_hidden_tab``); the message is shared so
    both surfaces give one answer, including the remedy.

    An unreadable state is "unknown", not "hidden": a page whose visibility
    cannot be read still gets its input.
    """
    if handler.page_visibility_state() != "hidden":
        return
    raise LifecycleError(HIDDEN_TAB_MESSAGE)


def snapshot(
    *,
    instance: str | None,
    target: str | None = None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
) -> dict[str, Any]:
    """Return the native UID accessibility tree (frozen ``take_snapshot``).

    Reading a background tab is fine -- only input is dropped -- so this is
    not focus-guarded. ``target`` selects the page whose UIDs are returned,
    which is what makes those UIDs usable with ``click --target``.
    """
    with _handler_for(handler, instance, target, registry_path, endpoint, all_frames) as handler:
        tree = _native_or_raise(handler, "take_snapshot", {})
    return {"snapshot": tree}


def click(
    *,
    instance: str | None,
    uid: str,
    target: str | None = None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Native UID click (frozen ``click``), over the #40 interaction path.

    Takes no snapshot. A UID carries the document it was minted against and the
    backend DOM node it names, so the interaction checks the document and acts.
    The internal snapshot this used to take is exactly what made the staleness
    check unable to fire: it was always "current", so a UID from a different
    tree resolved against it by ordinal and named whatever now sat there.
    """
    standalone = handler is None
    with _handler_for(handler, instance, target, registry_path, endpoint, all_frames, dialog=dialog) as handler:
        _refuse_input_to_hidden_tab(handler)
        text = _native_or_raise(handler, "click", {"uid": uid})
        records = handler.dialog_document() if standalone else {}
    return {"uid": uid, "result": text, **records}


def fill(
    *,
    instance: str | None,
    uid: str,
    text: str,
    target: str | None = None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Native UID fill (frozen ``fill``), over the #40 interaction path.

    Takes no snapshot, for the reason :func:`click` gives.

    Carries the dialog policy because setting a value fires the field's own
    ``input`` handler, and that handler can raise a dialog as directly as a
    click handler does. Measured before this carried it: ``fill`` against an
    ``<input oninput="alert(...)">`` never returned.
    """
    standalone = handler is None
    with _handler_for(handler, instance, target, registry_path, endpoint, all_frames, dialog=dialog) as handler:
        _refuse_input_to_hidden_tab(handler)
        result = _native_or_raise(handler, "fill", {"uid": uid, "value": text})
        records = handler.dialog_document() if standalone else {}
    return {"uid": uid, "text": text, "result": result, **records}


# ---------------------------------------------------------------------------
# Semantic waits (over CDPHandler.call_tool -> _handle_wait_idle/_handle_wait_stable)
# ---------------------------------------------------------------------------


def wait_idle(
    *,
    instance: str | None,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    idle_ms: int = DEFAULT_IDLE_MS,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
) -> dict[str, Any]:
    """Wait for network idle (frozen ``wait_idle``)."""
    with _handler_for(handler, instance, None, registry_path, endpoint) as handler:
        text = _tool_or_raise(handler, "wait_idle", {"timeout_ms": timeout_ms, "idle_ms": idle_ms})
    return {"result": text}


def wait_stable(
    *,
    instance: str | None,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    stable_ms: int = DEFAULT_STABLE_MS,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
) -> dict[str, Any]:
    """Wait for DOM quiescence (frozen ``wait_stable``)."""
    with _handler_for(handler, instance, None, registry_path, endpoint) as handler:
        text = _tool_or_raise(
            handler, "wait_stable", {"timeout_ms": timeout_ms, "stable_ms": stable_ms}
        )
    return {"result": text}


# ---------------------------------------------------------------------------
# Interstitial detection (over CDPHandler.run_post_navigation_detection -> interstitial.py)
# ---------------------------------------------------------------------------


#: Image formats ``Page.startScreencast`` accepts.
SCREENCAST_FORMATS = ("jpeg", "png")


def check_screencast_values(duration: float, fmt: str) -> None:
    """Reject a capture that cannot run, without opening a connection.

    Public because Step List validation calls it too: RFC-03 requires every
    step to be checked before the first one runs, and a check that lives
    only inside ``screencast`` would be reached after earlier steps had
    already driven the browser.
    """
    if duration <= 0:
        raise UsageError("screencast --duration must be greater than 0 seconds")
    if fmt not in SCREENCAST_FORMATS:
        raise UsageError("screencast --format must be 'jpeg' or 'png'")


def check_detect_wait(wait_seconds: float | None) -> None:
    """Reject a wait that cannot be turned into a retry budget.

    ``detect`` converts the wait with ``math.ceil``, which raises on NaN and
    on infinity. Unchecked, ``bt detect --wait nan`` exits 1 with a
    traceback; it is a malformed invocation, so it is exit 2 with a message.
    A negative wait is not rejected, because the conversion already floors
    the budget at zero.
    """
    if wait_seconds is None:
        return
    if not math.isfinite(wait_seconds):
        raise UsageError("detect --wait must be a finite number of seconds")


def detect(
    *,
    instance: str | None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    wait_seconds: float | None = None,
    handler: CDPHandler | None = None,
) -> dict[str, Any]:
    """Run interstitial detection against the current page.

    Drives the exact challenge-response policy in ``interstitial.py`` through
    ``CDPHandler.run_post_navigation_detection`` -- the same detect-and-retry
    the daemon runs automatically post-navigation, surfaced here as a verb.
    """
    check_detect_wait(wait_seconds)
    max_retries = None
    if wait_seconds is not None:
        max_retries = max(0, math.ceil(wait_seconds / INTERSTITIAL_RETRY_DELAY_SECONDS))
    with _handler_for(handler, instance, None, registry_path, endpoint) as handler:
        try:
            result = handler.run_post_navigation_detection(max_retries)
        except TimeoutError as exc:
            # Exit 1 either way, but the reason has to be the real one: alone
            # this is detection outrunning its own budget, and inside a run it
            # is the run's deadline, which the engine reads off the clock.
            raise LifecycleError("interstitial detection did not finish in time") from exc
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
        # Vendor-presence signals: this site uses these systems, but this page
        # is not a challenge. Informational only; never drives "am I blocked".
        "presence": result.get("presence", []),
        "auto_retried": result.get("auto_retried", False),
        "retries_used": result.get("retries_used", 0),
        "report": report,
    }


# ---------------------------------------------------------------------------
# Frames (over CDPHandler.call_tool -> list_frames/select_frame/reset_frame)
# ---------------------------------------------------------------------------


def frames_list(
    *,
    instance: str | None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
) -> dict[str, Any]:
    """List the page's frames (frozen ``list_frames``)."""
    with _handler_for(handler, instance, None, registry_path, endpoint, all_frames) as handler:
        text = _tool_or_raise(handler, "list_frames", {})
    return {"frames": text}


def frames_select(
    *,
    instance: str | None,
    pattern: str,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
) -> dict[str, Any]:
    """Select a frame by URL pattern (frozen ``select_frame``)."""
    with _handler_for(handler, instance, None, registry_path, endpoint, all_frames) as handler:
        text = _tool_or_raise(handler, "select_frame", {"url_pattern": pattern})
    return {"selected": text}


def frames_reset(
    *,
    instance: str | None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
) -> dict[str, Any]:
    """Clear frame selection back to the top-level page (frozen ``reset_frame``)."""
    with _handler_for(handler, instance, None, registry_path, endpoint, all_frames) as handler:
        text = _tool_or_raise(handler, "reset_frame", {})
    return {"result": text}


# ---------------------------------------------------------------------------
# Storage (over CDPHandler.call_tool -> get_frame_storage)
# ---------------------------------------------------------------------------


def storage_get(
    *,
    instance: str | None,
    key: str | None = None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
    all_frames: bool = False,
) -> dict[str, Any]:
    """Read a frame's storage (frozen ``get_frame_storage``).

    ``get_frame_storage`` reads the *selected* frame, and frame selection does
    not survive between one-shot CLI processes. ``--key`` names the frame to
    read (a URL pattern), selected for this read alone; omitting it surfaces
    the tool's own "No frame selected" error (exit 1).

    The selection is borrowed and given back, because inside a Step Run it is
    shared with every later step. See
    :meth:`CDPHandler.borrowed_frame_selection`.
    """
    with (
        _handler_for(handler, instance, None, registry_path, endpoint, all_frames) as handler,
        contextlib.ExitStack() as scope,
    ):
        if key:
            scope.enter_context(handler.borrowed_frame_selection())
            _tool_or_raise(handler, "select_frame", {"url_pattern": key})
        text = _tool_or_raise(handler, "get_frame_storage", {})
    return {"storage": text}


# ---------------------------------------------------------------------------
# Screencast (over CDPHandler.call_tool -> screencast_start/screencast_stop)
# ---------------------------------------------------------------------------


def screencast(
    *,
    instance: str | None,
    out_dir: str,
    duration: float = DEFAULT_SCREENCAST_DURATION_SECONDS,
    fmt: str = "jpeg",
    max_frames: int = DEFAULT_SCREENCAST_MAX_FRAMES,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
) -> dict[str, Any]:
    """Capture a bounded screencast and write its frames, in one invocation.

    Starts ``Page.startScreencast``, buffers the painted frames, writes them to
    ``out_dir`` with a ``frames.json`` manifest, and exits. Capture ends at
    whichever bound comes first: ``duration`` seconds, or ``max_frames``
    buffered frames.

    One invocation is the whole verb because the frame buffer is process-local
    (RFC-01 v6, "Screencast"). A ``screencast start`` in one CLI process
    buffers into a recorder that a ``screencast stop`` in a second process
    cannot reach, so the pair dispatched correctly and could never work. A
    detached recorder process was considered and declined: it would add a
    second long-lived process with its own lifetime, registry presence and
    exit-logging rules to serve a case this already covers. Nothing here
    outlives the invocation.

    Args:
        instance: Registered instance to capture from, omittable as everywhere.
        out_dir: Directory to write the frames and manifest into.
        duration: Seconds to capture for.
        fmt: ``jpeg`` or ``png``.
        max_frames: Buffer cap; reaching it ends the capture early.
        registry_path: Registry override.
        endpoint: Drive an external browser instead of a registered instance.

    Raises:
        UsageError: ``duration`` is not positive, or ``fmt`` is not jpeg/png.
        LifecycleError: The capture could not start, or the frames could not
            be written.
    """
    check_screencast_values(duration, fmt)

    with _handler_for(handler, instance, None, registry_path, endpoint) as handler:
        _tool_or_raise(handler, "screencast_start", {"format": fmt, "max_frames": max_frames})
        frames = _capture_for(handler, duration, max_frames)
        text = _tool_or_raise(handler, "screencast_stop", {"dir": out_dir})
    return {"frames": frames, "dir": out_dir, "result": text}


def _capture_for(handler: CDPHandler, duration: float, max_frames: int) -> int:
    """Let the recorder buffer frames, and return how many it got.

    Returns as soon as the buffer is full rather than waiting out ``duration``:
    once ``max_frames`` is reached the recorder stops acking, so CDP flow
    control pauses the stream and no further frame can arrive.

    Inside a Step Run it also stops at the run's deadline. This loop waits in
    the calling thread with nothing in flight, so it is the one wait
    ``bounded_timeout`` cannot reach, and without this a `--timeout 3` run
    would sit through a `--duration 20` capture. The frames already buffered
    are kept and written: the capture did happen, and a run that stops does
    not undo what its steps already did.
    """
    deadline = time.monotonic() + duration
    if handler.deadline is not None:
        deadline = min(deadline, handler.deadline)
    while time.monotonic() < deadline:
        if handler.screencast_frame_count >= max_frames:
            break
        time.sleep(SCREENCAST_POLL_SECONDS)
    return handler.screencast_frame_count


# ---------------------------------------------------------------------------
# Screenshot (session transport: Page.captureScreenshot + blank-frame guard)
# ---------------------------------------------------------------------------


async def _capture_screenshot(
    port: int, target_spec: str | None, target_by: str | None, *, external: bool = False
) -> str:
    """Capture a full-page PNG over a one-shot session, guarding blank frames.

    Opens the one-shot page session (the same seam ``passthrough``/``events``
    use) and sends ``Page.captureScreenshot`` over it. Reuses the existing
    blank-frame guard (``screenshot_utils.screenshot_looks_blank`` plus the
    shared retry budget): a near-uniform capture is retried after a short
    delay, matching the daemon's ``take_screenshot`` post-capture check.
    """
    async with one_shot_page_session(port, target_spec, target_by, external=external) as (
        cdp,
        session_id,
    ):
        return await capture_on_session(cdp, session_id)


async def capture_on_session(cdp: Any, session_id: str) -> str:
    """Capture one full-page PNG over a session someone else opened.

    Split out so a Step Run can reach it: the run holds one session for every
    step, so it cannot call a function that opens its own. The bare verb still
    goes through this, so there is one capture implementation, not two.
    """
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
    endpoint: str | None = None,
    handler: CDPHandler | None = None,
) -> dict[str, Any]:
    """Capture a page screenshot (frozen ``take_screenshot``, CDP-native form).

    ``handler`` is a session already open, which a Step Run holds for every
    step. This verb runs over a One-Shot Session rather than over the
    handler's tool surface, so it takes the handler's session pair and
    submits the capture to the handler's loop. The rendering below, which is
    what the caller actually sees, is the same either way.

    ``--path`` writes the PNG to a file; without it the base64 ``data:`` URI is
    returned. ``--target``/``--url`` pick the page target, as on the passthrough
    line. Target-resolution and CDP failures become ``LifecycleError`` (exit 1),
    via ``@cli_cdp_errors``.
    """
    target_selector(target, url)  # refuses --target with --url, before anything opens

    if handler is not None:
        cdp, session_id = handler.require_session()
        data = handler.submit(capture_on_session(cdp, session_id))
    else:
        port = _resolve_port(instance, registry_path, endpoint)
        spec, target_by = target_selector(target, url)
        data = asyncio.run(
            _capture_screenshot(port, spec, target_by, external=endpoint is not None)
        )

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


class DialogCancelledError(LifecycleError):
    """An action that failed after the dialog policy answered something.

    Carries the dialog records so the caller is told why. Without this the
    records are collected only on the success path, and the one case the
    `navigate` verb exists for -- a beforeunload prompt that the default
    `dismiss` declines, cancelling the navigation -- reported a bare
    `net::ERR_ABORTED` with nothing saying a prompt had been declined.
    """

    def __init__(self, message: str, document: dict[str, Any]) -> None:
        super().__init__(message)
        self.document = document


def _action(
    name: str, arguments: dict[str, Any], *, instance: str | None,
    target: str | None, url: str | None, registry_path: str | None,
    endpoint: str | None, handler: CDPHandler | None, all_frames: bool,
    dialog: str | None = "dismiss",
) -> dict[str, Any]:
    with _handler_for(handler, instance, target, registry_path, endpoint, all_frames, url, dialog) as opened:
        if name in {"press", "hover", "type"}:
            _refuse_input_to_hidden_tab(opened)
        try:
            result = json.loads(_native_or_raise(opened, name, arguments))
        except LifecycleError as exc:
            if handler is not None or dialog is None:
                raise
            records = opened.dialog_document()
            if not records.get("dialogs"):
                raise
            declined = [
                entry for entry in records["dialogs"]
                if entry.get("type") == "beforeunload" and entry.get("answer") == "dismiss"
            ]
            if declined:
                raise DialogCancelledError(
                    f"{exc}: the page asked to confirm leaving and --dialog "
                    f"{dialog} declined it, which cancels the navigation. Pass "
                    "--dialog accept to leave the page.",
                    records,
                ) from exc
            raise DialogCancelledError(str(exc), records) from exc
        if handler is None and dialog is not None:
            result.update(opened.dialog_document())
        return result


def eval_js(
    *, instance: str | None, source: str, await_promise: bool = False,
    target: str | None = None, url: str | None = None,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Evaluate an expression or statement body; a JS throw is exit 1."""
    return _action("eval", {"source": source, "await_promise": await_promise},
                   instance=instance, target=target, url=url, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)


def press(
    *, instance: str | None, key: str, modifiers: str | None = None,
    target: str | None = None, url: str | None = None,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Dispatch a keyDown/keyUp pair with the fields default actions need."""
    from .curated_runtime import key_event

    key_event(key, modifiers)
    return _action("press", {"key": key, "modifiers": modifiers},
                   instance=instance, target=target, url=url, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)


def hover(
    *, instance: str | None, uid: str,
    target: str | None = None, url: str | None = None,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Move the native pointer over the live UID's box, including CSS :hover."""
    return _action("hover", {"uid": uid},
                   instance=instance, target=target, url=url, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)


def type_text(
    *, instance: str | None, text: str | None = None, file: str | None = None,
    target: str | None = None, url: str | None = None,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Insert positional or UTF-8 file text at the caret with one insertText."""
    if (text is None) == (file is None):
        raise UsageError("type requires exactly one of text or --file FILE")
    arguments: dict[str, Any] = {"source": "text"}
    if file is not None:
        try:
            text = sys.stdin.read() if file == "-" else Path(file).read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise LifecycleError(f"cannot read type --file {file}: {exc}") from exc
        arguments.update(source="file", path=file if file == "-" else str(Path(file).resolve()))
    arguments["text"] = text
    return _action("type", arguments,
                   instance=instance, target=target, url=url, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)


def wait_text(
    *, instance: str | None, substring: str, timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    target: str | None = None, url: str | None = None,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Wait for a substring in rendered text without a check/subscription gap."""
    if timeout_ms < 0:
        raise UsageError("wait-text --timeout-ms must be non-negative")
    return _action("wait-text", {"substring": substring, "timeout_ms": timeout_ms},
                   instance=instance, target=target, url=url, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)


def network_get(
    *, instance: str | None, url: str | None = None, request_id: str | None = None,
    response_file: str | None = None, target: str | None = None,
    duration: float = 2.0, reload: bool = False,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Fetch a response without navigating, with an optional standalone reload.

    Carries the dialog policy because ``--reload`` sends ``Page.reload``, which
    runs ``beforeunload``. This is the same omission that left ``fill`` able to
    hang: the rule is every verb that drives the page, and ``--reload`` drives
    it.
    """
    if (url is None) == (request_id is None):
        raise UsageError("network-get requires exactly one of --url SUB or --request-id ID")
    import math

    if not math.isfinite(duration) or duration < 0:
        raise UsageError("network-get --duration must be finite and non-negative")
    if handler is not None and reload:
        raise UsageError("network-get --reload is standalone only")
    return _action("network-get", {"url": url, "request_id": request_id,
                                  "response_file": response_file,
                                  "duration": duration, "reload": reload},
                   instance=instance, target=target, url=None, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)


def navigate(
    *, instance: str | None, destination: str,
    target: str | None = None, url: str | None = None,
    registry_path: str | None = None, endpoint: str | None = None,
    handler: CDPHandler | None = None, all_frames: bool = False,
    dialog: str = "dismiss",
) -> dict[str, Any]:
    """Navigate the page, answering beforeunload with the invocation policy."""
    return _action("navigate", {"url": destination},
                   instance=instance, target=target, url=url, registry_path=registry_path,
                   endpoint=endpoint, handler=handler, all_frames=all_frames, dialog=dialog)
