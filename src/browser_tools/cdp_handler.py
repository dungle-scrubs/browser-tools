#!/usr/bin/env python3
"""CDP handler and toolset definitions for browser-tools daemon.

Tool handler methods for CDP domain operations: frame management,
accessibility, content extraction, export, and screencast.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

    from .curated_runtime import ResponseBuffer

from .core.attach import AmbiguousTargetError, TargetNotFoundError
from .dialog_policy import DialogPolicy
from .endpoint import ResolvedEndpoint
from .lifecycle import LifecycleError

logger = logging.getLogger(__name__)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The loop on this thread, or None when this thread runs no loop."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


#: Connection failures that are outcomes rather than defects: no browser on
#: the port, and a target spec that names no page or more than one. They get a
#: one-line message; everything else keeps its traceback.
_EXPECTED_CONNECT_FAILURES = (
    ConnectionError,
    AmbiguousTargetError,
    TargetNotFoundError,
)

try:
    from .cdp_constants import (
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_PAINT_READY_TIMEOUT_MS,
    )
    from .interstitial import (
        detect_interstitials_async,
        detect_total_timeout,
        detect_with_retry,
    )
    from .mcp_response import make_error, make_text
    from .native_interaction import NativeInteractor, UidResolutionError
    from .native_snapshot import NativeSnapshotReader
    from .screencast import ScreencastRecorder
    from .tool_registry import CDP_TOOLS
except ImportError:
    from cdp_constants import (  # type: ignore[import-untyped,no-redef]
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_PAINT_READY_TIMEOUT_MS,
    )
    from interstitial import (  # type: ignore[import-untyped,no-redef]
        detect_interstitials_async,
        detect_total_timeout,
        detect_with_retry,
    )
    from mcp_response import (  # type: ignore[import-untyped,no-redef]
        make_error,
        make_text,
    )
    from native_interaction import (  # type: ignore[import-untyped,no-redef]
        NativeInteractor,
        UidResolutionError,
    )
    from native_snapshot import NativeSnapshotReader  # type: ignore[import-untyped,no-redef]
    from screencast import ScreencastRecorder  # type: ignore[import-untyped,no-redef]
    from tool_registry import CDP_TOOLS  # type: ignore[import-untyped,no-redef]


class ToolInvocationError(Exception):
    """Raised when a CDP-backed tool fails during execution.

    Wraps the underlying cause so callers get the original exception
    chain plus a tool-call-scoped context.
    """

    def __init__(self, method: str, cause: BaseException) -> None:
        super().__init__(f"{method}: {cause}")
        self.method = method
        self.cause = cause


class CdpToolError(Exception):
    """A CDP tool call failed; carries the label for the error response.

    ``str()`` yields ``"<label> failed"`` (unexpected) or
    ``"<label> failed: <cause>"`` (a wrapped CDP protocol failure), which
    ``call_tool``'s top-level handler turns into the matching ``make_error``
    text. Raised by :func:`_cdp_call` so handlers stay linear.
    """

    def __init__(self, label: str, cause: BaseException | None = None) -> None:
        self.label = label
        self.cause = cause
        if cause is not None:
            super().__init__(f"{label} failed: {cause}")
        else:
            super().__init__(f"{label} failed")


def _get_cdp_error_class() -> type[Exception]:
    """Import CDPError lazily to avoid breaking script-mode execution.

    cdp_client.py uses relative imports that fail when modules are
    loaded outside of the browser_tools package (e.g. --help flag
    parsing in the daemon script).  Deferring the import until a CDP
    call is actually attempted keeps the module importable in all
    execution modes.
    """
    try:
        from .cdp_client import CDPError  # type: ignore[import-untyped,reportMissingImports]
    except ImportError:
        from cdp_client import (  # type: ignore[import-untyped,reportMissingImports]
            CDPError,  # type: ignore[no-redef]
        )
    return CDPError


async def _safe_cdp_send(
    cdp_client: Any, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send a CDP command, raising ToolInvocationError on failure.

    Args:
        cdp_client: Connected CDPClient.
        method: CDP method name.
        params: Optional parameters dict.

    Returns:
        Parsed result dict from CDP.

    Raises:
        ToolInvocationError: Wraps CDPError (expected CDP protocol
            failures) so callers can distinguish them from unexpected
            exceptions.
    """
    CDPError = _get_cdp_error_class()
    try:
        if params is not None:
            return await cdp_client.send(method, params)
        return await cdp_client.send(method)
    except CDPError as exc:
        raise ToolInvocationError(method, exc) from exc


async def _cdp_call(
    cdp_client: Any,
    method: str,
    params: dict[str, Any] | None,
    *,
    label: str,
) -> dict[str, Any]:
    """Send one CDP command, raising :class:`CdpToolError` on failure.

    Consolidates the error mapping that was duplicated in every handler:
    ``ToolInvocationError`` (an expected CDP protocol failure) and any
    unexpected exception both become a ``CdpToolError`` carrying ``label``.
    ``call_tool``'s top-level ``except`` turns it into the matching
    ``make_error`` text, so handlers stay linear with no per-call try/except.
    """
    try:
        if params is not None:
            return await _safe_cdp_send(cdp_client, method, params)
        return await _safe_cdp_send(cdp_client, method)
    except ToolInvocationError as exc:
        raise CdpToolError(label, exc.cause) from exc
    except Exception:
        logger.exception("Unexpected error in %s", label)
        raise CdpToolError(label) from None


@dataclass
class ElementEvalResult:
    """Result of evaluating a JS expression against a CSS-selected element.

    ``found`` is True only when the selector matched an element; ``value``
    holds the expression's return value, which may be None when the expression
    itself yields null (for example an absent attribute on a present element).
    Element-not-found is reported as ``found=False`` so callers never overload
    a null value to mean "missing element" - the bug that previously forced
    each handler to invent its own sentinel string.
    """

    found: bool
    value: Any


# The querySelector + not-found wrapper shared by every element-reading tool.
# Uses a namespaced ``__found__`` key so a returned object that happens to have
# a ``found`` field cannot be misread as the protocol envelope.
_ELEM_NOT_FOUND_JS = "return {__found__: false};"

# element_visible: evaluated against the matched element (``el``) via
# eval_on_element. A selector that matches nothing never reaches this - it is
# reported as found=False upstream - so the expression assumes a real element.
_VISIBILITY_EXPR = """(function (el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none') return false;
    if (style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
})(el)"""


def _element_eval_js(selector: str, expression: str) -> str:
    """Build the IIFE that resolves ``selector`` and evaluates ``expression``.

    ``expression`` runs with ``el`` bound to the matched element and may return
    any JSON-serializable value, including null.
    """
    return f"""
    (() => {{
        const el = document.querySelector({selector!r});
        if (!el) {{ {_ELEM_NOT_FOUND_JS} }}
        return {{__found__: true, value: ({expression})}};
    }})()
    """


async def eval_on_element(
    cdp: Any, selector: str, expression: str, *, label: str
) -> ElementEvalResult:
    """Evaluate ``expression`` against the element matching ``selector``.

    Single owner of the querySelector wrapper, the Runtime.evaluate call, and
    the not-found contract. Before this, six handlers reimplemented the same
    idea in three incompatible styles: a None return, sentinel strings
    (``__ELEMENT_NOT_FOUND__`` / ``__ATTR_NULL__``), and inline JS returning
    ``false`` / ``0``. One interface here replaces all three.

    Args:
        cdp: Connected CDPClient.
        selector: CSS selector string.
        expression: JS expression evaluated with ``el`` bound to the matched
            element. May return any JSON-serializable value, including null.
        label: Error-mapping label forwarded to :func:`_cdp_call`.

    Returns:
        ``ElementEvalResult`` with ``found=False`` when the selector matched
        nothing, otherwise ``found=True`` and the expression's value.

    Raises:
        CdpToolError: on CDP protocol failure (mapped by ``_cdp_call``).
    """
    result = await _cdp_call(
        cdp,
        "Runtime.evaluate",
        {"expression": _element_eval_js(selector, expression), "returnByValue": True},
        label=label,
    )
    val = result.get("result", {}).get("value")
    if isinstance(val, dict) and val.get("__found__") is True:
        return ElementEvalResult(found=True, value=val.get("value"))
    return ElementEvalResult(found=False, value=None)


# Tool name -> handler method name. This is the single binding of CDP tool name
# to handler; it is parity-checked against tool_registry.CDP_TOOLS so the
# registry remains the single source of which tools are CDP-routed. Adding a
# CDP tool takes exactly one edit here plus the handler method - no elif chain
# to keep in sync.
_CDP_HANDLERS: dict[str, str] = {
    "list_frames": "_handle_list_frames",
    "select_frame": "_handle_select_frame",
    "reset_frame": "_handle_reset_frame",
    "get_frame_events": "_handle_get_frame_events",
    "get_frame_storage": "_handle_get_frame_storage",
    "ax_find": "_handle_ax_find",
    "ax_node": "_handle_ax_node",
    "export_pdf": "_handle_export_pdf",
    "screenshot_element": "_handle_screenshot_element",
    "screencast_start": "_handle_screencast_start",
    "screencast_stop": "_handle_screencast_stop",
    "wait_idle": "_handle_wait_idle",
    "wait_stable": "_handle_wait_stable",
    "get_text": "_handle_get_text",
    "get_html": "_handle_get_html",
    "get_attr": "_handle_get_attr",
    "element_exists": "_handle_element_exists",
    "element_visible": "_handle_element_visible",
}

assert set(_CDP_HANDLERS) == CDP_TOOLS, (
    "CDP handler table drifted from tool_registry.CDP_TOOLS; "
    f"symmetric difference: {set(_CDP_HANDLERS) ^ set(CDP_TOOLS)}"
)


#: What a CDP call gets once a Step Run's deadline has passed. Enough for a
#: step to undo what it started (stop a screencast, disable a domain), and
#: not enough to carry on working past the deadline the caller set.
DEADLINE_GRACE_SECONDS = 5.0

#: The least any one call gets once even the grace is spent. A call handed
#: zero fails before it is sent, which defeats the grace; a call handed this
#: can still send a `Page.stopScreencast` and hear back.
TEARDOWN_FLOOR_SECONDS = 0.25

#: How long to wait on the one `Runtime.evaluate` behind the focus guard.
VISIBILITY_READ_TIMEOUT_SECONDS = 5.0


class CDPRuntime:
    """Owns the CDP background thread, event loop, WebSocket connection, frame
    manager, screencast recorder, and the thread-safe marshal methods the Daemon
    calls (``await_paint_ready``, ``run_post_navigation_detection``).

    The deep half of the CDP layer: a lot of machinery (thread + loop +
    WebSocket connection + frame-tree subscriptions + two marshal hooks) behind
    a small surface. :class:`CDPHandler` sits above it and reaches the browser
    through ``client`` / ``frame_manager`` / ``screencast`` rather than owning
    them, so the two depths - runtime versus tool handlers - have a seam
    instead of sharing one class.
    """

    # Declared on the class, not only in `__init__`, so every runtime reaching
    # `bounded_timeout` has them. Tests build a runtime with `__new__` to drive
    # one method without a browser behind it, and a bound that is only an
    # instance attribute raises `AttributeError` there instead of returning the
    # caller's own timeout.
    _target_by: str | None = None
    _deadline: float | None = None
    _grace_until: float | None = None
    #: Rebound rather than mutated, so the class-level default cannot be
    #: shared by two runtimes built without `__init__`.
    _caller_enabled: frozenset[str] = frozenset()
    _all_frames: bool = False
    _frame_sessions: Any = None
    network_responses: dict[str, ResponseBuffer] | None = None
    dialog_policy: DialogPolicy | None = None

    def __init__(
        self,
        browser_url: str | ResolvedEndpoint | None,
        mode: str = "full",
        stealth: bool = False,
        target_spec: str | None = None,
        target_by: str | None = None,
        all_frames: bool = False,
    ) -> None:
        """Initialize the CDP runtime.

        Args:
            browser_url: Chrome remote debugging URL.
            mode: Access mode ('full' or 'inspect').
            target_spec: Which page to attach to, as an index into the page
                targets sorted by target ID, or a target ID. None takes the
                first page in that order, which is what ``--target 0`` names.
            stealth: Accepted for MCP surface compatibility only. No JavaScript
                is injected for fingerprint purposes (RFC-01, "Anti-detection":
                each JS override is independently detectable). Chrome
                fingerprinting is launch-flag profiles (``core/fingerprint.py``)
                via ``launch --fingerprint``; Camoufox is the engine-level path
                via ``launch --engine camoufox``.
        """
        self._browser_url = browser_url
        self._mode = mode
        self._stealth = stealth
        self._target_spec = target_spec
        self._target_by = target_by
        # RFC-04, Decision 8: off unless the caller asked for it. A cross-
        # origin iframe is reached only with `--frames all`, for the first
        # release, because the correctness work behind it is new.
        self._all_frames = all_frames
        self._frame_sessions: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._cdp_client: Any = None
        self._frame_manager: Any = None
        # Holds the One-Shot Session open for the whole runtime: every step of
        # a Step Run shares it (RFC-03), so it cannot be scoped to one command.
        self._sessions: Any = None
        self._connect_error: str | None = None
        # A Step Run's whole-run deadline, as a monotonic instant. Every wait
        # on this runtime clamps to what is left of it, so the run's
        # `--timeout` reaches the step in flight instead of only being
        # checked between steps.
        self._deadline: float | None = None
        self._grace_until: float | None = None
        self._caller_enabled: frozenset[str] = frozenset()
        # Screencast capture state machine (Page.startScreencast buffer, ack,
        # write-to-dir). Owned here so the screencast tool handlers stay
        # two-line delegators.
        self._screencast = ScreencastRecorder()

    @property
    def available(self) -> bool:
        """Whether the CDP client is connected and setup has finished.

        `_ready` is the second half, and it is not decoration. The caller in
        `curated._cdp_handler_session` polls this property to decide the
        invocation may start, and `_connect_cdp` assigns `_cdp_client` as its
        first statement - before `Page.enable`, before the frame tree, before
        `Runtime.enable`, before Frame Sessions. Without `_ready` the poll
        returns true in the gap and the verb runs against a half-built map.

        It stayed hidden because the poll sleeps 20 ms and setup usually
        beats it. Setup that does more does not: on a page with twenty
        cross-origin iframes, `frames list --frames all` returned 20, then
        8, then 20, then 4 out-of-process frames over consecutive identical
        runs, each answer being however much of the map existed when the
        read won the race.
        """
        return self._cdp_client is not None and self._cdp_client.connected and self._ready.is_set()

    @property
    def connect_error(self) -> str | None:
        """Why the connection failed, once it has. ``None`` until then."""
        return self._connect_error

    @property
    def session(self) -> tuple[Any, str] | None:
        """The run's ``(client, sessionId)``, or None before it connects."""
        if self._cdp_client is None:
            return None
        return self._cdp_client.raw, self._cdp_client.session_id

    def set_deadline(self, deadline: float | None) -> None:
        """Bound every later wait on this runtime by one monotonic instant."""
        self._deadline = deadline
        self._grace_until = None

    @property
    def deadline(self) -> float | None:
        """The run's deadline, for a step that waits without a call in flight.

        `bounded_timeout` can only shorten a wait that is waiting on CDP. A
        step that polls locally, as the screencast capture does, has nothing
        in flight to cut, so it reads the deadline and clamps its own.
        """
        return self._deadline

    @property
    def frame_sessions(self) -> Any:
        """Every Frame Session below the page session (RFC-04).

        ``None`` when the caller did not ask for them with ``--frames all``,
        and when the browser refused ``Target.setAutoAttach``.
        """
        return self._frame_sessions

    @property
    def caller_enabled(self) -> frozenset[str]:
        """Domains a raw step in this run turned on by hand.

        A later curated step must not disable one of these on its way out.
        CDP cannot tell the helper who enabled a domain - a second
        `Network.enable` succeeds exactly like a first - so the run records
        it at the one place that knows: the step that sent the enable.
        """
        return self._caller_enabled

    def record_caller_enable(self, method: str) -> None:
        """Note a raw step's own ``Domain.enable`` so nothing undoes it."""
        domain, _, call = method.partition(".")
        if call == "enable" and domain:
            self._caller_enabled = self._caller_enabled | {domain}

    def bounded_timeout(self, timeout: float | None) -> float | None:
        """``timeout`` clamped to what is left of the run's deadline.

        A deadline bounds work, not teardown. Once it has passed, the call
        being made is a step unwinding - and refusing that call leaves the
        browser in whatever state the step was halfway through. A
        `screencast` cut off at the deadline would never reach
        `Page.stopScreencast`, so the capture it started would go on running
        on a page nobody is watching. So a passed deadline returns a small
        fixed budget rather than raising.

        The run still stops, and not because of this method: the call that
        was in flight when the deadline passed is cut short by its own
        clamped bound, and `step_run.execute` checks the clock before it
        starts each step.
        """
        if self._deadline is None:
            return timeout
        now = time.monotonic()
        remaining = self._deadline - now
        if remaining > 0:
            return remaining if timeout is None else min(timeout, remaining)

        # One budget for the whole unwind, opened by the first call after the
        # deadline. Handing every call a fresh `DEADLINE_GRACE_SECONDS` would
        # bound no call and the run together: a step making four calls while
        # it unwinds could run four graces past a deadline the caller set.
        if self._grace_until is None:
            self._grace_until = now + DEADLINE_GRACE_SECONDS
        # Clamped to the grace itself, not just derived from it. `now + g - now`
        # is not exactly `g` in binary floating point once `time.monotonic()`
        # is large: on a CI runner the first call after the deadline came back
        # with 5.000000000000014 against a grace of 5.0. The excess cannot
        # matter to a timeout, but "no call gets more than one grace" is the
        # rule this method exists to enforce, and a rule that holds to within
        # a rounding error is a different rule.
        left = min(self._grace_until - now, DEADLINE_GRACE_SECONDS)
        # Never zero. A call given no time at all fails before it is sent, and
        # then the teardown this grace exists for does not happen either.
        return max(left, TEARDOWN_FLOOR_SECONDS)

    def submit(self, coro: Any, timeout: float | None = None) -> Any:
        """Run one coroutine on this runtime's loop and return its result.

        The loop belongs to the runtime's background thread, so a caller on
        another thread cannot await the coroutine itself. A Step Run uses
        this for the One-Shot Session verbs, which are coroutines over the
        session this runtime already holds.
        """
        try:
            if self._loop is None:
                raise RuntimeError("the CDP runtime is not running")
            if _running_loop() is self._loop:
                # The loop would have to run this coroutine and wait for it at
                # the same time. It cannot, so the wait never ends.
                raise RuntimeError("submit was called from the runtime's own loop")
            bound = self.bounded_timeout(timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except BaseException:
            # Nothing took ownership of the coroutine, so nothing will ever
            # await it. Left alone it emits "coroutine was never awaited" on
            # stderr, at the exact moment a run is already reporting why it
            # failed.
            coro.close()
            raise

        try:
            return future.result(timeout=bound)
        except BaseException:
            # The wait ended; the coroutine did not. Abandoning it leaves work
            # running on the session the next step is about to use, and on the
            # one teardown is about to close. Cancelling reaches the coroutine
            # and runs its cleanup. What it already sent to the browser was
            # sent; this stops what has not happened yet.
            future.cancel()
            raise

    @property
    def mode(self) -> str:
        """Current access mode ('full' or 'inspect')."""
        return self._mode

    @property
    def client(self) -> Any:
        """The connected CDP client (or None), exposed for the tool handlers."""
        return self._cdp_client

    @property
    def frame_manager(self) -> Any:
        """The frame manager, exposed for the frame tool handlers."""
        return self._frame_manager

    @property
    def screencast(self) -> ScreencastRecorder:
        """The screencast recorder, exposed for the screencast tool handlers."""
        return self._screencast

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The background event loop, exposed for thread-safe dispatch."""
        return self._loop

    def cdp_or_error(self) -> tuple[Any, dict[str, Any] | None]:
        """Return the connected CDP client, or an error response if down.

        Replaces the hand-copied ``if cdp is None or not cdp.connected`` guards.
        Returns ``(cdp_client, None)`` when connected, otherwise
        ``(None, make_error(...))``.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return None, make_error("CDP client not connected")
        return cdp, None

    def run(self) -> None:
        """Run the asyncio event loop (called in a background thread)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        """Main async entry point."""
        import contextlib as _contextlib

        from .frame_manager import FrameManager

        self._frame_manager = FrameManager()
        self._sessions = _contextlib.AsyncExitStack()

        try:
            if self._browser_url:
                await self._connect_cdp()

            self._ready.set()

            if self._stop_event:
                await self._stop_event.wait()
        finally:
            # `finally`, not a straight line, because cancellation would
            # otherwise skip the detach and leave the session attached and the
            # socket open. CancelledError is a BaseException, so the suppress
            # below does not catch it either.
            if self._cdp_client:
                await self._cdp_client.disconnect()
            # Detaches the Target session and closes the browser-level socket.
            # Best-effort: a browser that died first must not turn teardown
            # into a traceback on a command that already returned its result.
            with _contextlib.suppress(Exception):
                await self._sessions.aclose()

    def _target_selector(self) -> tuple[str, str]:
        """Translate this runtime's target spec for the One-Shot Session.

        Returns ``(target_spec, target_by)``, both always set. The seam
        dispatches on ``target_by`` and rejects ``None``, so a spec cannot
        travel without one.

        No spec means the first page, and it has to be said explicitly.
        ``resolve_page_ws_url`` answered a missing spec with ``pages[0]``;
        ``resolve_target`` answers it with ``AmbiguousTargetError`` unless
        exactly one page exists. Passing the absence straight through would
        break every handler-routed verb the moment a second tab or a popup
        is open, and ``frames``, ``storage`` and ``detect`` have no
        ``--target`` to escape with. Both paths sort page targets by target
        ID, so index 1 is the page ``pages[0]`` was.
        """
        if self._target_by is not None:
            return self._target_spec or "", self._target_by
        if self._target_spec is None:
            return "1", "index"
        return self._target_spec, ("index" if self._target_spec.isdigit() else "id")

    async def _connect_cdp(self) -> None:
        """Attach a Target session and build the runtime over it.

        Entered on ``self._sessions`` rather than with ``async with``: the
        session has to outlive this function and last as long as the runtime.
        """
        from urllib.parse import urlparse

        from .attached_session import AttachedSessionClient
        from .one_shot import one_shot_page_session

        browser_url = self._browser_url
        port = browser_url if isinstance(browser_url, ResolvedEndpoint) else urlparse(browser_url or "").port
        if port is None:
            self._connect_error = f"no port in browser url {browser_url}"
            logger.error("%s", self._connect_error)
            return

        target_spec, target_by = self._target_selector()
        try:
            cdp, session_id = await self._sessions.enter_async_context(
                one_shot_page_session(port, target_spec, target_by)
            )
        except _EXPECTED_CONNECT_FAILURES as exc:
            # A browser that is not there, or a target spec that names no
            # page, is an ordinary outcome. The caller reports it from
            # `connect_error`, so logging it again here would print it twice.
            self._connect_error = str(exc)
            logger.debug("CDP connection failed for %s: %s", browser_url, exc)
            return
        except Exception as exc:
            self._connect_error = str(exc)
            logger.exception("CDP connection failed for %s", browser_url)
            return

        try:
            self._cdp_client = AttachedSessionClient(cdp, session_id)
            if self.dialog_policy is not None:
                self.dialog_policy.start(self._cdp_client)
                self._sessions.push_async_callback(self.dialog_policy.close)

            # SUBSCRIBE FIRST, then enable. `Runtime.enable` replays the
            # execution contexts that already exist, and enabling before the
            # handler was registered threw that replay away: every frame kept
            # `execution_context_id = None`, and `storage get` then returned
            # cookies only, with no localStorage or sessionStorage, and exit 0.
            # The same discipline the event verbs already follow.
            self._cdp_client.on("Page.frameAttached", self._frame_manager.handle_frame_attached)
            self._cdp_client.on("Page.frameDetached", self._frame_manager.handle_frame_detached)
            self._cdp_client.on("Page.frameNavigated", self._frame_manager.handle_frame_navigated)
            self._cdp_client.on(
                "Page.navigatedWithinDocument",
                self._frame_manager.handle_navigated_within_document,
            )
            await self._cdp_client.send("Page.enable")

            # The frame tree before the Runtime replay, not after: the context
            # handler files each context against a frame it looks up by id, and
            # `update_from_frame_tree` clears the map it would look in.
            result = await self._cdp_client.send("Page.getFrameTree")
            if "frameTree" in result:
                self._frame_manager.update_from_frame_tree(result["frameTree"])

            self._cdp_client.on(
                "Runtime.executionContextCreated",
                self._frame_manager.handle_execution_context_created,
            )
            self._cdp_client.on(
                "Runtime.executionContextDestroyed",
                self._frame_manager.handle_execution_context_destroyed,
            )
            self._cdp_client.on(
                "Runtime.executionContextsCleared",
                self._frame_manager.handle_execution_contexts_cleared,
            )
            await self._cdp_client.send("Runtime.enable")

            # RFC-04. Last, and only when asked: a page with no cross-origin
            # iframe pays nothing, and a failure here must not cost the
            # caller the page session that is already working.
            if self._all_frames:
                await self._start_frame_sessions(cdp, session_id)

            # Setup is over, so the invocation may start. Set here rather
            # than in `_main`, because this is the method that finishes the
            # work `available` promises has been done, and it is also called
            # on its own.
            self._ready.set()
        except Exception as exc:
            self._connect_error = str(exc)
            logger.exception("CDP session setup failed for %s", self._browser_url)
            self._cdp_client = None

    async def start_run_network(self) -> None:
        """Subscribe before step 1, retaining responses for this runtime only."""
        self.network_responses = {}
        await self.capture_network_session(self._cdp_client.session_id)
        if self._frame_sessions is not None:
            self._frame_sessions.on_session_ready = self.capture_network_session
            for session in list(self._frame_sessions.sessions.values()):
                if session.ready:
                    await self.capture_network_session(session.session_id)

    async def capture_network_session(self, session_id: str) -> None:
        from .attached_session import AttachedSessionClient
        from .curated_runtime import ResponseBuffer

        assert self.network_responses is not None
        if session_id in self.network_responses:
            return
        cdp = AttachedSessionClient(self._cdp_client.raw, session_id)
        buffer = ResponseBuffer()
        self.network_responses[session_id] = buffer
        await self._sessions.enter_async_context(buffer.capture(cdp, self.caller_enabled))

    async def _start_frame_sessions(self, cdp: Any, session_id: str) -> None:
        """Turn auto-attach on, or carry on without it.

        A browser that refuses `Target.setAutoAttach` leaves the page session
        exactly as it was, and `frames select` keeps the diagnosis that says
        a cross-origin iframe is out of reach. That is a worse answer than
        reaching the frame and a much better one than no page at all.
        """
        from .frame_sessions import FrameSessions

        try:
            # Which target this session is attached to. `one_shot_page_session`
            # knows it and yields `(cdp, session_id)`, a shape six callers
            # unpack, so it is asked for here rather than threaded through all
            # of them. One round trip, on the opt-in path only.
            info = await cdp.send(method="Target.getTargetInfo", session_id=session_id)
            target_id = (info.get("targetInfo") or {}).get("targetId", "")
            sessions = FrameSessions(cdp, self._frame_manager, session_id, target_id)
            await sessions.start()
            self._frame_sessions = sessions
            # Discovery before the invocation reports itself ready. Without
            # this, `frames select` on an Out-of-Process Frame races the
            # splice and fails against a tree the child is not in yet.
            await sessions.settle()
        except Exception as exc:
            logger.debug("auto-attach unavailable, driving the page session alone: %s", exc)
            self._frame_sessions = None

    def stop(self) -> None:
        """Signal the background loop to stop."""
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def await_paint_ready(self, timeout_ms: int = SCREENSHOT_PAINT_READY_TIMEOUT_MS) -> bool:
        """Block until Chrome has painted at least one stable frame.

        Used as a pre-capture gate for take_screenshot. Waits for two
        requestAnimationFrame ticks (one to flush the current frame, one to
        confirm the next frame committed) plus document.fonts.ready, so any
        in-flight CSS transition / font swap / layout reflow has reached
        the compositor before we capture.

        DOM-mutation waits (wait_stable) miss this case because CSS
        transform/opacity animations don't fire MutationObserver callbacks
        -- the DOM looks settled while pixels are still moving.

        Returns:
            True if the gate completed; False if CDP is unavailable or the
            evaluate timed out. Never raises -- screenshot must still proceed
            on a best-effort basis if this gate fails.
        """
        if not self._loop or not self._cdp_client or not self._cdp_client.connected:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self._await_paint_ready_async(timeout_ms), self._loop
        )
        try:
            return future.result(timeout=self.bounded_timeout((timeout_ms / 1000.0) + 2.0))
        except Exception:
            logger.debug("await_paint_ready timed out or failed", exc_info=True)
            return False

    async def _await_paint_ready_async(self, timeout_ms: int) -> bool:
        """Run the rAF + fonts.ready gate via Runtime.evaluate.

        The JS resolves once the next frame after the current one has been
        scheduled by the compositor, with a hard timeout safety net so a
        broken page (background tab, no rAF firing) can't hang the daemon.
        """
        js = f"""
        (async () => {{
            const timeout = new Promise(r => setTimeout(() => r('timeout'), {int(timeout_ms)}));
            const paint = new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r('paint'))));
            const fonts = (document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve();
            await Promise.race([Promise.all([paint, fonts]), timeout]);
            return true;
        }})()
        """
        try:
            await self._cdp_client.send(
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            return True
        except Exception:
            logger.debug("_await_paint_ready_async failed", exc_info=True)
            return False

    def page_visibility_state(self) -> str | None:
        """Read ``document.visibilityState`` of the attached page (thread-safe).

        Chrome drops input sent to a background tab without an error, so a
        verb that delivers input asks this first. Returns "visible",
        "hidden", or None when the state cannot be read -- None means
        "unknown", and an unknown state must not block a working page.
        """
        loop = self._loop
        client = self._cdp_client
        if loop is None or client is None or not client.connected:
            return None

        async def _read() -> Any:
            return await client.send(
                "Runtime.evaluate",
                {"expression": "document.visibilityState", "returnByValue": True},
            )

        try:
            result = asyncio.run_coroutine_threadsafe(_read(), loop).result(
                timeout=self.bounded_timeout(VISIBILITY_READ_TIMEOUT_SECONDS)
            )
        except Exception:
            logger.debug("page_visibility_state failed", exc_info=True)
            return None
        value = result.get("result", {}).get("value")
        return value if isinstance(value, str) else None

    def run_post_navigation_detection(
        self, max_retries: int | None = None
    ) -> dict[str, Any] | None:
        """Run post-navigation interstitial detection (thread-safe).

        Delegates the detect-and-retry policy to :mod:`interstitial`; this
        method only marshals that coroutine onto the CDP event loop and enforces
        the outer timeout. The retry tuning and the single-shot detection both
        live in the interstitial module, so the whole challenge-response policy
        has one home rather than being smeared across the runtime, interstitial,
        and cdp_constants.

        Returns:
            Dict with 'detections', 'auto_retried', 'retries_used', or None if
            CDP is unavailable.
        """
        if not self._loop or not self._cdp_client or not self._cdp_client.connected:
            return None

        async def _detect_once() -> list[dict[str, Any]]:
            return await detect_interstitials_async(self._cdp_client)

        async def _run() -> dict[str, Any]:
            return await detect_with_retry(_detect_once, max_retries=max_retries)

        future = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        try:
            return future.result(timeout=self.bounded_timeout(detect_total_timeout(max_retries)))
        except TimeoutError:
            # Not swallowed into None. None means "no session to detect on",
            # and reporting a run that ran out of time as a missing session
            # sends the caller looking for a browser that is right there.
            future.cancel()
            raise
        except Exception:
            logger.debug("run_post_navigation_detection failed", exc_info=True)
            return None


class CDPHandler:
    """CDP tool handler registry: routes the 18 CDP tools to their handlers, each
    executed on the :class:`CDPRuntime` event loop.

    Owns no connection state itself - the runtime does - and reaches the browser
    through the runtime's ``client`` / ``frame_manager`` / ``screencast`` seam.
    The Daemon constructs this, threads ``run``, and calls the two cross-cutting
    hooks (``await_paint_ready``, ``run_post_navigation_detection``), all of which
    delegate to the runtime, their natural owner.
    """

    def __init__(
        self,
        browser_url: str | ResolvedEndpoint | None,
        mode: str = "full",
        stealth: bool = False,
        target_spec: str | None = None,
        target_by: str | None = None,
        all_frames: bool = False,
    ) -> None:
        """Initialize the handler registry over a fresh CDP runtime.

        Args:
            browser_url: Chrome remote debugging URL.
            mode: Access mode ('full' or 'inspect').
            stealth: Accepted for MCP surface compatibility only; see
                :class:`CDPRuntime`. No JavaScript is injected.
            target_spec: Which page to attach to; see :class:`CDPRuntime`.
        """
        self._rt = CDPRuntime(
            browser_url,
            mode,
            stealth=stealth,
            target_spec=target_spec,
            target_by=target_by,
            all_frames=all_frames,
        )
        # Tool name -> bound handler. Built from the class-level _CDP_HANDLERS
        # table, which is parity-checked against tool_registry.CDP_TOOLS above.
        self._handlers: dict[str, Any] = {
            name: getattr(self, method) for name, method in _CDP_HANDLERS.items()
        }
        # Native snapshot/UID backend (RFC-01 Phase 2, ticket #41). One reader
        # per handler owns the snapshot lifecycle for this session; the
        # interactor resolves a UID against the reader's current snapshot, so a
        # ``take_snapshot`` populates the UIDs a subsequent ``click``/``fill``
        # resolves. This is the default snapshot backend; ``--engine mcp`` keeps
        # the Node path (these methods are then never reached).
        self._native_reader = NativeSnapshotReader()
        self._native_interactor = NativeInteractor(self._native_reader)

    # --- Daemon-facing surface: delegate to the runtime ---
    @property
    def available(self) -> bool:
        """Whether the runtime's CDP client is connected and ready."""
        return self._rt.available

    @property
    def connect_error(self) -> str | None:
        """Why the runtime's connection failed, once it has."""
        return self._rt.connect_error

    @property
    def session(self) -> tuple[Any, str] | None:
        """The runtime's ``(client, sessionId)``, or None before it connects."""
        return self._rt.session

    def set_deadline(self, deadline: float | None) -> None:
        """Bound every later wait on this handler; see :meth:`CDPRuntime.set_deadline`."""
        self._rt.set_deadline(deadline)

    @property
    def deadline(self) -> float | None:
        """The run's deadline; see :attr:`CDPRuntime.deadline`."""
        return self._rt.deadline

    @property
    def caller_enabled(self) -> frozenset[str]:
        """Domains a raw step turned on by hand; see :attr:`CDPRuntime.caller_enabled`."""
        return self._rt.caller_enabled

    @property
    def frame_sessions(self) -> Any:
        """The Frame Sessions below the page session; see :class:`CDPRuntime`."""
        return self._rt.frame_sessions

    def record_caller_enable(self, method: str) -> None:
        """See :meth:`CDPRuntime.record_caller_enable`."""
        self._rt.record_caller_enable(method)

    def configure_dialog_policy(self, value: str) -> None:
        """Set the invocation policy before the runtime thread starts."""
        self._rt.dialog_policy = DialogPolicy(value)

    def dialog_document(self) -> dict[str, Any]:
        """Wait only for observed answers and return their ordered records."""
        policy = self._rt.dialog_policy
        return self.submit(policy.document()) if policy is not None else {}

    def start_run_network(self) -> None:
        """Enable and retain network responses before the first step runs."""
        self.submit(self._rt.start_run_network())

    def require_session(self) -> tuple[Any, str]:
        """The runtime's ``(client, sessionId)``, or a refusal saying why not.

        A verb handed this handler is about to send on it. ``session`` is
        ``None`` before the connection comes up, and the pair alone is not a
        liveness guarantee: it survives a disconnect while ``available`` is
        false. Unpacking it blind turns either case into a `TypeError` about
        `NoneType` at the point of use, which says nothing about the browser.
        """
        pair = self._rt.session
        if pair is None or not self.available:
            reason = self.connect_error or "the CDP session is not connected"
            raise LifecycleError(reason)
        return pair

    def submit(self, coro: Any, timeout: float | None = None) -> Any:
        """Run one coroutine on the runtime's loop; see :meth:`CDPRuntime.submit`."""
        return self._rt.submit(coro, timeout=timeout)

    @property
    def mode(self) -> str:
        """Current access mode ('full' or 'inspect')."""
        return self._rt.mode

    def run(self) -> None:
        """Run the CDP runtime's event loop (called in a background thread)."""
        self._rt.run()

    def stop(self) -> None:
        """Signal the CDP runtime's background loop to stop."""
        self._rt.stop()

    def await_paint_ready(self, timeout_ms: int = SCREENSHOT_PAINT_READY_TIMEOUT_MS) -> bool:
        """Block until Chrome has painted a stable frame (delegates to runtime)."""
        return self._rt.await_paint_ready(timeout_ms)

    def run_post_navigation_detection(
        self, max_retries: int | None = None
    ) -> dict[str, Any] | None:
        """Run post-navigation interstitial detection (delegates to runtime)."""
        return self._rt.run_post_navigation_detection(max_retries)

    def page_visibility_state(self) -> str | None:
        """Read the attached page's visibility state (delegates to runtime)."""
        return self._rt.page_visibility_state()

    # --- Runtime state, exposed to the handlers as a documented seam. These
    #     read through to the runtime so the handlers access the browser via a
    #     stable surface instead of owning connection state. ---
    @property
    def _cdp_client(self) -> Any:
        return self._rt.client

    @property
    def _frame_manager(self) -> Any:
        return self._rt.frame_manager

    @contextlib.contextmanager
    def borrowed_frame_selection(self) -> Generator[None]:
        """Select inside this block, and give the run's selection back after.

        A `--key` on a read names the frame for that one read. In a one-shot
        invocation the difference does not show, because the selection dies
        with the process a moment later. In a Step Run the selection outlives
        the step, so a read that left it moved would silently re-point every
        later frame-scoped step at whatever frame it happened to read.

        `GUIDE.txt` states the rule this keeps true: only `frames reset` and
        `frames select` change the pattern.
        """
        frames = self._frame_manager
        pattern = frames.selection_pattern if frames is not None else None
        try:
            yield
        finally:
            if frames is not None:
                frames.restore_selection(pattern)

    @property
    def _screencast(self) -> ScreencastRecorder:
        return self._rt.screencast

    @property
    def screencast_frame_count(self) -> int:
        """How many screencast frames are buffered right now.

        Public because a bounded ``bt screencast`` capture polls it to end as
        soon as the frame cap is reached (#99). The recorder itself stays
        private: nothing outside needs to drive it.
        """
        return self._rt.screencast.frame_count

    @property
    def _loop(self) -> asyncio.AbstractEventLoop | None:
        return self._rt.loop

    def _cdp_or_error(self) -> tuple[Any, dict[str, Any] | None]:
        """Return the connected CDP client, or an error response if down."""
        return self._rt.cdp_or_error()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a CDP/frame tool (thread-safe, blocks until complete).

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        if not self._loop or not self._loop.is_running():
            return make_error("CDP handler not initialized")

        future = asyncio.run_coroutine_threadsafe(self._dispatch_tool(name, arguments), self._loop)
        try:
            return future.result(timeout=self._rt.bounded_timeout(REQUEST_TIMEOUT_SECONDS))
        except Exception as exc:
            logger.warning("call_tool(%s) error: %s", name, exc)
            future.cancel()
            # A bare TimeoutError stringifies to "", which would reach the
            # caller as the word "Error" and nothing else.
            return make_error(str(exc) or f"{name} did not answer in time")

    def call_native(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a native snapshot/UID tool on the CDP loop (thread-safe).

        The flipped default backend for ``take_snapshot`` / ``click`` / ``fill``
        (RFC-01 Phase 2, #41). It runs the native read/interaction path built in
        #39/#40 over this session's CDP client and returns the same bare MCP
        envelope shape the Node engine returned, so the frozen tool response
        shape is preserved. Mirrors :meth:`call_tool`'s loop scheduling.

        Args:
            name: One of ``take_snapshot``, ``click``, ``fill``.
            arguments: Tool arguments (``uid``/``value`` for interactions).

        Returns:
            JSON-RPC style response dict.
        """
        if not self._loop or not self._loop.is_running():
            return make_error("CDP handler not initialized")

        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_native(name, arguments), self._loop
        )
        try:
            timeout = REQUEST_TIMEOUT_SECONDS
            if name == "wait-text":
                timeout = max(timeout, arguments.get("timeout_ms", 5000) / 1000 + 5)
            elif name == "network-get":
                timeout = max(timeout, arguments.get("duration", 2.0) + 5)
            return future.result(timeout=self._rt.bounded_timeout(timeout))
        except Exception as exc:
            logger.warning("call_native(%s) error: %s", name, exc)
            future.cancel()
            # A bare TimeoutError stringifies to "", which would reach the
            # caller as the word "Error" and nothing else.
            return make_error(str(exc) or f"{name} did not answer in time")

    def mark_native_navigation(self) -> None:
        """Invalidate the native snapshot's UIDs after a navigation.

        A navigation replaces the document, so every outstanding native UID is
        stale. Called by the daemon dispatcher after a navigation tool so a UID
        from a pre-navigation snapshot no longer resolves (native's stability
        contract). No-op cost when the Node engine is selected.
        """
        self._native_reader.note_navigation()

    async def _dispatch_native(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a native tool call to the snapshot or interaction path."""
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err
        send = cdp.send

        if name in {"eval", "press", "hover", "type", "wait-text", "network-get"}:
            return await self._dispatch_curated_action(cdp, name, arguments)

        if name == "navigate":
            result = await cdp.send("Page.navigate", arguments)
            if result.get("errorText"):
                # make_error, not raise: this was the only branch here that
                # raised, and call_native's generic handler logs every
                # exception through logger.warning. The project installs no
                # logging handlers, so logging.lastResort printed
                # "call_native(navigate) error: ..." to stderr on every failed
                # navigation, naming an internal function beside the real
                # message.
                return make_error(result["errorText"])
            return make_text(json.dumps(result))

        if name == "take_snapshot":
            documents = self._cross_process_documents()
            if documents:
                merged, token = await self._stitch_across_sessions(documents)
                snapshot = self._native_reader.build(merged, doc_token=token)
            else:
                snapshot = await self._native_reader.snapshot_stitched(send)
            return make_text(snapshot.format_tree())

        if name in ("click", "fill"):
            uid = str(arguments.get("uid", "")).strip()
            if not uid:
                return make_error(f"E008: '{name}' requires --uid UID from a prior 'snapshot'")
            send = self._send_for_uid(uid, send)
            try:
                if name == "click":
                    result = await self._native_interactor.click_async(send, uid)
                    assert result.point is not None  # click_steps always sets point
                    return make_text(
                        f"Clicked uid={result.uid} at ({result.point[0]:.0f}, {result.point[1]:.0f})."
                    )
                value = str(arguments.get("value", ""))
                result = await self._native_interactor.fill_async(send, uid, value)
                return make_text(f"Filled uid={result.uid} (value now {result.value_after!r}).")
            except UidResolutionError as exc:
                return make_error(await self._uid_failure(uid, exc))

        return make_error(f"Unknown native tool: {name}")

    async def _dispatch_curated_action(
        self, cdp: Any, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from . import curated_runtime as actions

        page_cdp = cdp
        fm = self._frame_manager
        selected = fm.get_selected_frame() if fm is not None else None
        context_id: int | None = None
        if fm is not None and fm.selection_pattern is not None:
            context = fm.get_selected_context()
            if selected is None or context is None:
                return make_error("selected frame has no live execution context; select it again")
            frame_session, context_id = context
            cdp = self.client_for_session(frame_session)

        if name == "hover":
            uid = arguments["uid"]
            if selected is not None:
                from .native_snapshot import doc_token, parse_uid

                token, _ = parse_uid(uid)
                if token != doc_token(selected.frame_id, selected.loader_id):
                    return make_error("hover UID does not belong to the selected frame")
            send = self._send_for_uid(uid, cdp.send)
            try:
                point = await self._native_interactor.hover_async(send, uid)
            except UidResolutionError as exc:
                return make_error(await self._uid_failure(uid, exc))
            return make_text(json.dumps({"uid": uid, "x": point[0], "y": point[1]}))
        if name == "eval":
            budget = self._rt.bounded_timeout(REQUEST_TIMEOUT_SECONDS)
            timeout_ms = int((budget if budget is not None else REQUEST_TIMEOUT_SECONDS) * 1000)
            result = await actions.evaluate(
                cdp,
                arguments["source"],
                arguments["await_promise"],
                context_id,
                timeout_ms=timeout_ms,
                page_cdp=page_cdp,
            )
        elif name == "press":
            await actions.focus_selected(cdp, context_id)
            params, names = actions.key_event(arguments["key"], arguments.get("modifiers"))
            await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **params})
            await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **params, "text": ""})
            result = {
                "key": arguments["key"],
                "modifiers": names,
                "dispatched": ["keyDown", "keyUp"],
            }
        elif name == "type":
            await actions.focus_selected(cdp, context_id)
            await actions.refuse_type_without_a_target(cdp, context_id)
            await cdp.send("Input.insertText", {"text": arguments["text"]})
            result = {"chars": len(arguments["text"]), "source": arguments["source"]}
            if arguments.get("path") is not None:
                result["path"] = arguments["path"]
        elif name == "wait-text":
            timeout_ms = arguments["timeout_ms"]
            if self.deadline is not None:
                timeout_ms = min(timeout_ms, max(0, int((self.deadline - time.monotonic()) * 1000)))
            result = await actions.wait_text(cdp, arguments["substring"], timeout_ms, context_id)
        else:
            result = await actions.network_get(
                cdp,
                arguments.get("url"),
                arguments.get("request_id"),
                arguments.get("response_file"),
                selected.frame_id if selected else None,
                selected.url if selected else None,
                self.caller_enabled,
                duration=arguments.get("duration", actions.DEFAULT_NETWORK_DURATION),
                reload=arguments.get("reload", False),
                buffer=(
                    self._rt.network_responses[cdp.session_id]
                    if self._rt.network_responses is not None
                    else None
                ),
            )
        return make_text(json.dumps(result))

    async def _uid_failure(self, uid: str, exc: UidResolutionError) -> str:
        """Why the UID did not resolve, saying which of the reasons it was.

        The resolver knows one reason and states it: the document is gone.
        There is a second, and it has the opposite remedy. A UID minted
        inside a cross-origin iframe names that iframe's document, and this
        invocation only sees documents it attached to - so without
        `--frames all` a perfectly live UID is refused with "the page
        navigated since", and taking another snapshot produces the same UID
        and the same refusal.

        Asking the browser for its `iframe` targets is what tells the two
        apart. On the failure path only, and a browser that will not answer
        leaves the plain message rather than an error about the diagnosis.
        This is the same shape as the `frames select` diagnosis for #127.
        """
        message = str(exc)
        if self._rt.frame_sessions is not None or "previous document" not in message:
            return message
        targets = await self._out_of_process_frames("")
        if not targets:
            return message
        listed = ", ".join(targets[:3])
        return (
            f"{message}\n"
            f"Or the uid names a node inside a cross-origin iframe, and this "
            f"command did not pass --frames all. The browser has "
            f"{len(targets)} such iframe(s): {listed}. A uid minted with "
            f"--frames all only resolves with --frames all."
        )

    def _cross_process_documents(self) -> list[Any]:
        """The page's documents, when any of them is in another process.

        Empty when every frame answers on the page session, which is the
        common page and the whole of the default `--frames page`. The
        same-process stitch then runs unchanged: it is one session, one
        `Page.getFrameTree`, and no frame map needed.
        """
        fm = self._frame_manager
        if fm is None or self._rt.frame_sessions is None:
            return []
        documents = fm.frame_documents()
        if not any(document.session_id for document in documents):
            return []
        return documents

    async def _stitch_across_sessions(self, documents: list[Any]) -> tuple[dict[str, Any], str]:
        """Read each frame's accessibility tree on the session that owns it.

        `Accessibility.getFullAXTree` answers for the renderer it is sent to.
        A cross-origin iframe is a different renderer, so asking the page
        session for it returns nothing and the `Iframe` node stays empty -
        which is exactly what `snapshot` printed before this.

        `DOM.getFrameOwner` goes to the *parent*, because the owner node is
        in the parent's document. Each frame is read with its own token, so
        the merged tree carries several documents and `build` mints each
        node's UID against the one it came from.

        A frame that will not answer is skipped, not fatal: one broken child
        frame degrades the snapshot to the rest of the tree, the same way the
        same-process stitch already degrades.
        """
        from .native_snapshot import (
            AX_ENABLE,
            AX_GET_FULL_TREE,
            DOM_GET_FRAME_OWNER,
            ChildFrameTree,
            stitch_ax_frames,
        )

        top, rest = documents[0], documents[1:]
        top_send = self.client_for_session(top.session_id).send
        await top_send(AX_ENABLE)
        top_tree = await top_send(AX_GET_FULL_TREE)

        children: list[ChildFrameTree] = []
        for document in rest:
            if document.parent_doc_token is None:
                continue
            try:
                owner_send = self.client_for_session(document.parent_session_id).send
                owner = await owner_send(DOM_GET_FRAME_OWNER, {"frameId": document.frame_id})
                backend = owner.get("backendNodeId")
                if not isinstance(backend, int):
                    continue
                child_send = self.client_for_session(document.session_id).send
                await child_send(AX_ENABLE)
                # No frameId: the child's own session answers for its own
                # frame, and passing one asks that renderer about a frame it
                # does not own.
                tree = await child_send(AX_GET_FULL_TREE)
            except Exception:
                logger.debug("frame %s did not answer for its tree", document.frame_id)
                continue
            children.append(
                ChildFrameTree(
                    owner_backend_node_id=backend,
                    owner_doc_token=document.parent_doc_token,
                    doc_token=document.doc_token,
                    result=tree,
                )
            )

        return (
            stitch_ax_frames(top_tree, children, top_doc_token=top.doc_token),
            top.doc_token,
        )

    def _send_for_uid(self, uid: str, default: Any) -> Any:
        """The transport for the document a UID was minted in.

        A backend DOM node id is unique within a renderer process, not within
        a browser, so `DOM.resolveNode` for a cross-origin frame's node has to
        be sent to that frame's own session. Sent to the page session it either
        fails or, worse, resolves a different node that happens to share the
        id - measured, a cross-origin child's ids overlapped the page's on 5
        of its 7 nodes.

        A UID whose token names no frame in the map falls back to the default
        transport, so the staleness error comes from the resolver with its own
        wording rather than from a routing failure here.
        """
        from .native_snapshot import parse_uid

        fm = self._frame_manager
        if fm is None or self._rt.frame_sessions is None:
            return default
        token, _ = parse_uid(uid)
        for document in fm.frame_documents():
            if document.doc_token == token and document.session_id:
                return self.client_for_session(document.session_id).send
        return default

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to its registered handler.

        The handler table (``_CDP_HANDLERS``) is the single binding of tool name
        to method; it is parity-checked against ``tool_registry.CDP_TOOLS`` at
        import, so a tool flagged CDP in the registry with no handler here (or
        vice versa) fails loudly rather than silently returning "Unknown".

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        handler = self._handlers.get(name)
        if handler is None:
            return make_error(f"Unknown CDP tool: {name}")
        return await handler(arguments)

    async def _handle_list_frames(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle list_frames tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        frames = fm.get_flat_frames()
        if not frames:
            if not (self._cdp_client and self._cdp_client.connected):
                return make_text("No frames available. CDP client not connected.")
            # An empty tree usually means nothing has refreshed it yet. Refresh
            # and answer from the result: this handler runs on the MCP dispatch
            # path, not the CDP read loop, so awaiting here cannot deadlock.
            # Firing a detached task and telling the caller to ask again left
            # the result unobserved and cost a second round trip.
            await self._refresh_frame_tree()
            frames = fm.get_flat_frames()
            if not frames:
                return make_text("No frames available.")

        lines = ["Frames in current page:\n"]
        for frame in frames:
            indent = "  " * frame["depth"]
            selected = " [selected]" if frame["frameId"] == fm.selected_frame_id else ""
            name = f' name="{frame["name"]}"' if frame.get("name") else ""
            # A frame answered by its own session is out-of-process. Say so:
            # it is why its UIDs come from a different document, and why it is
            # absent altogether without `--frames all` (RFC-04).
            oopif = " [out-of-process]" if frame.get("frameSessionId") else ""
            lines.append(f"{indent}{frame['frameId']}: {frame['url']}{name}{oopif}{selected}")

        # A frame a bound refused is listed and marked, never silently
        # dropped: a caller who cannot see it cannot ask why it is missing.
        sessions = self._rt.frame_sessions
        if sessions is not None:
            for target_id, url in sessions.unreachable.items():
                lines.append(f"  {target_id}: {url or '(url unknown)'} [unreachable]")
        return make_text("\n".join(lines))

    async def _handle_select_frame(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle select_frame tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        url_pattern = arguments.get("url_pattern", "")
        if not url_pattern:
            return make_error("url_pattern is required")

        frame = fm.select_frame_by_url(url_pattern)
        if frame is None:
            return make_error(await self._no_frame_matched(url_pattern))

        context = fm.get_selected_context()
        if context is None:
            ctx_info = " (no execution context yet)"
        else:
            session_id, ctx_id = context
            where = "page session" if session_id is None else f"frame session {session_id}"
            ctx_info = f", executionContextId={ctx_id} on the {where}"
        return make_text(
            f"Selected frame: {frame.frame_id}\n"
            f"URL: {frame.url}\n"
            f"Origin: {frame.security_origin}{ctx_info}"
        )

    def client_for_session(self, frame_session_id: str | None) -> Any:
        """The client to send a frame-scoped command on.

        A command against a frame in another renderer has to go to that
        renderer's Frame Session. Sending it on the page session with the
        child's `contextId` fails, because a context id is unique within one
        renderer and means nothing outside it.

        ``None`` is the page session, which is every frame's answer until an
        Out-of-Process Frame is attached. A second `AttachedSessionClient`
        over the same connection is the whole adapter: it registers nothing
        and owns nothing, it only carries the session id onto each send.
        """
        if frame_session_id is None or self._cdp_client is None:
            return self._cdp_client
        from .attached_session import AttachedSessionClient

        return AttachedSessionClient(self._cdp_client.raw, frame_session_id)

    async def _out_of_process_frames(self, url_pattern: str) -> list[str]:
        """URLs of `iframe` targets matching the pattern, newest first.

        A cross-origin iframe runs in its own renderer process under Chrome's
        site isolation, which gives it its own CDP target. It is not in
        `Page.getFrameTree` on the page session and its lifecycle events do
        not arrive there, so the frame manager cannot see it at all.

        Asking the browser is the only way to tell "no such frame" apart from
        "a frame this tool cannot reach". The call is on the failure path
        only, and a browser that will not answer it leaves the caller with
        the plain message rather than an error about the diagnosis.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return []
        try:
            result = await cdp.send(method="Target.getTargets")
        except (_get_cdp_error_class(), ConnectionError, TimeoutError, OSError):
            return []
        lowered = url_pattern.lower()
        return [
            info.get("url", "")
            for info in result.get("targetInfos", [])
            if info.get("type") == "iframe" and lowered in info.get("url", "").lower()
        ]

    async def _no_frame_matched(self, url_pattern: str) -> str:
        """Why no frame matched, saying which of the two reasons it was."""
        out_of_process = await self._out_of_process_frames(url_pattern)
        if not out_of_process:
            return (
                f"E002: No frame found matching '{url_pattern}'. "
                "Run 'frames list' to see available frames."
            )
        listed = ", ".join(out_of_process[:3])
        return (
            f"E002: No frame found matching '{url_pattern}'. The browser has "
            f"a cross-origin iframe at {listed}, and this tool cannot reach "
            "it: site isolation puts it in its own process with its own CDP "
            "target, so it is absent from 'frames list' and 'snapshot' shows "
            "the Iframe node with nothing under it. There is no flag for "
            "this yet and --target does not reach it either. Drive the "
            "iframe's URL as a page instead, or work above it. See "
            "https://github.com/dungle-scrubs/browser-tools/issues/127."
        )

    async def _handle_reset_frame(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle reset_frame tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        fm.reset_frame()
        return make_text("Frame selection cleared. Now targeting top-level page.")

    async def _handle_get_frame_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle get_frame_events tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        events = fm.drain_events()
        if not events:
            return make_text("No frame events since last check.")

        lines = [f"{len(events)} frame event(s):\n"]
        for evt in events:
            url_info = f" → {evt['url']}" if evt.get("url") else ""
            lines.append(f"  [{evt['type']}] {evt['frameId']}{url_info}")
        return make_text("\n".join(lines))

    async def _handle_get_frame_storage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle get_frame_storage tool."""
        fm = self._frame_manager
        cdp = self._cdp_client
        if fm is None or cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selected = fm.get_selected_frame()
        if selected is None:
            return make_error(
                "No frame selected. Pass --key PATTERN to name the frame on this "
                "read, or select one first. Inside a 'bt run' a 'frames select' "
                "step governs the steps after it, and a navigation that leaves "
                "the pattern matching no frame clears it. Between separate "
                "commands a selection never carries over: it belongs to the "
                "process that made it."
            )

        storage_types = arguments.get(
            "storage_types", ["cookies", "localStorage", "sessionStorage"]
        )
        context = fm.get_selected_context()
        # The evaluate goes to the frame's own renderer. `cdp` stays the page
        # session for `Network.getCookies`, which is browser-scoped and takes
        # a URL rather than a context.
        ctx_id = context[1] if context else None
        frame_cdp = self.client_for_session(context[0]) if context else cdp
        result_parts: list[str] = []

        if "cookies" in storage_types:
            try:
                cookies_result = await _safe_cdp_send(
                    cdp, "Network.getCookies", {"urls": [selected.url]}
                )
                cookies = cookies_result.get("cookies", [])
                result_parts.append(f"Cookies ({len(cookies)}):")
                for c in cookies[:20]:
                    result_parts.append(f"  {c.get('name')}: {c.get('value', '')[:50]}")
            except ToolInvocationError as exc:
                result_parts.append(f"Cookies: error - {exc.cause}")
            except Exception:
                logger.exception("Storage fetch error (Cookies)")
                result_parts.append("Cookies: error - unexpected")

        if ctx_id and "localStorage" in storage_types:
            try:
                ls_result = await _safe_cdp_send(
                    frame_cdp,
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(localStorage).slice(0, 20)))",
                        "contextId": ctx_id,
                        "returnByValue": True,
                    },
                )
                ls_value = ls_result.get("result", {}).get("value", "{}")
                result_parts.append(f"\nlocalStorage: {ls_value}")
            except ToolInvocationError as exc:
                result_parts.append(f"\nlocalStorage: error - {exc.cause}")
            except Exception:
                logger.exception("Storage fetch error (localStorage)")
                result_parts.append("\nlocalStorage: error - unexpected")

        if ctx_id and "sessionStorage" in storage_types:
            try:
                ss_result = await _safe_cdp_send(
                    frame_cdp,
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(sessionStorage).slice(0, 20)))",
                        "contextId": ctx_id,
                        "returnByValue": True,
                    },
                )
                ss_value = ss_result.get("result", {}).get("value", "{}")
                result_parts.append(f"\nsessionStorage: {ss_value}")
            except ToolInvocationError as exc:
                result_parts.append(f"\nsessionStorage: error - {exc.cause}")
            except Exception:
                logger.exception("Storage fetch error (sessionStorage)")

                result_parts.append("\nsessionStorage: error - unexpected")

        if not result_parts:
            return make_text("No storage data retrieved.")

        return make_text("\n".join(result_parts))

    # ------------------------------------------------------------------ #
    # Accessibility tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_ax_find(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Find accessibility nodes by role and/or accessible name.

        Args:
            arguments: Tool arguments with optional 'role' and 'name' keys.

        Returns:
            JSON-RPC style response dict with list of matching AX nodes.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        role = arguments.get("role", "").strip()
        name = arguments.get("name", "").strip()

        if not role and not name:
            return make_error("E007: ax_find requires at least one of 'role' or 'name'")

        params: dict[str, Any] = {}
        if role:
            params["role"] = role
        if name:
            params["accessibleName"] = name

        result = await _cdp_call(
            cdp, "Accessibility.queryAXTree", params, label="Accessibility.queryAXTree"
        )

        nodes = result.get("nodes", [])
        # Filter out ignored nodes
        visible_nodes = [n for n in nodes if not n.get("ignored", False)]

        if not visible_nodes:
            criteria = []
            if role:
                criteria.append(f"role={role!r}")
            if name:
                criteria.append(f"name={name!r}")
            return make_text(f"No accessibility nodes found matching {', '.join(criteria)}.")

        lines = [f"{len(visible_nodes)} node(s) found:\n"]
        for node in visible_nodes:
            node_role = node.get("role", {}).get("value", "unknown")
            node_name = node.get("name", {}).get("value", "")
            node_id = node.get("nodeId", "")
            backend_id = node.get("backendDOMNodeId", "")

            # Extract key properties
            props = {}
            for prop in node.get("properties", []):
                k = prop.get("name", "")
                v = prop.get("value", {}).get("value", "")
                if k in ("disabled", "checked", "expanded", "required", "selected", "focused"):
                    props[k] = v

            prop_str = ""
            if props:
                prop_str = " " + " ".join(f"{k}={v}" for k, v in props.items())

            name_str = f' "{node_name}"' if node_name else ""
            lines.append(
                f"  [{node_role}]{name_str}{prop_str} (nodeId={node_id} backendDOMNodeId={backend_id})"
            )

        return make_text("\n".join(lines))

    async def _handle_ax_node(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Inspect a single element's accessibility properties.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with AX node properties.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        # Resolve selector to a backend DOM node ID
        eval_result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {"expression": f"document.querySelector({selector!r})", "returnByValue": False},
            label="Runtime.evaluate",
        )

        remote_obj = eval_result.get("result", {})
        if remote_obj.get("type") == "undefined" or remote_obj.get("subtype") == "null":
            return make_error(f"E006: No element found matching selector '{selector}'")

        object_id = remote_obj.get("objectId")
        if not object_id:
            return make_error(f"E006: No element found matching selector '{selector}'")

        # Get the backend DOM node ID
        node_result = await _cdp_call(
            cdp, "DOM.requestNode", {"objectId": object_id}, label="DOM.requestNode"
        )

        backend_node_id = node_result.get("nodeId")
        if not backend_node_id:
            return make_error("Could not resolve element to DOM node")

        # Get partial AX tree for this node
        ax_result = await _cdp_call(
            cdp,
            "Accessibility.getPartialAXTree",
            {"nodeId": backend_node_id, "fetchRelatives": False},
            label="Accessibility.getPartialAXTree",
        )

        nodes = ax_result.get("nodes", [])
        if not nodes:
            return make_text(f"No accessibility info found for selector '{selector}'")

        # Find the primary node (the one we requested)
        node = nodes[0]
        for n in nodes:
            if n.get("backendDOMNodeId") == backend_node_id:
                node = n
                break

        role = node.get("role", {}).get("value", "unknown")
        name = node.get("name", {}).get("value", "")
        description = node.get("description", {}).get("value", "")
        ignored = node.get("ignored", False)

        lines = [
            f"Accessibility properties for '{selector}':",
            f"  role: {role}",
            f"  name: {name!r}",
        ]
        if description:
            lines.append(f"  description: {description!r}")
        if ignored:
            lines.append("  ignored: true (not in accessibility tree)")

        for prop in node.get("properties", []):
            k = prop.get("name", "")
            v = prop.get("value", {}).get("value", "")
            lines.append(f"  {k}: {v}")

        return make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Page export / capture tools                                          #
    # ------------------------------------------------------------------ #

    async def _handle_export_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Export the current page as a PDF file.

        Args:
            arguments: Tool arguments with optional 'path', 'landscape',
                'print_background' keys.

        Returns:
            JSON-RPC style response dict with output file path.
        """
        import time

        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        landscape = arguments.get("landscape", False)
        print_background = arguments.get("print_background", True)
        out_path = arguments.get("path", "")
        if not out_path:
            out_path = f"{int(time.time())}_page.pdf"

        result = await _cdp_call(
            cdp,
            "Page.printToPDF",
            {
                "landscape": landscape,
                "printBackground": print_background,
                "transferMode": "ReturnAsBase64",
            },
            label="Page.printToPDF",
        )

        pdf_data = result.get("data", "")
        if not pdf_data:
            return make_error("No PDF data returned from Chrome")

        try:
            pdf_bytes = base64.b64decode(pdf_data)
            abs_path = str(Path(out_path).resolve())
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_bytes(pdf_bytes)
        except Exception:
            logger.exception("Unexpected error writing PDF to '%s'", out_path)
            return make_error(f"E009: Failed to write PDF to '{out_path}'")

        return make_text(f"PDF saved to: {abs_path} ({len(pdf_bytes):,} bytes)")

    async def _handle_screenshot_element(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Capture a screenshot of a specific element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' and optional 'path' keys.

        Returns:
            JSON-RPC style response dict with base64 image and optional file path.
        """

        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        # Scroll element into view and get bounding rect
        js = f"""
        (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return null;
            el.scrollIntoView({{block: 'center'}});
            const r = el.getBoundingClientRect();
            return {{x: r.x, y: r.y, width: r.width, height: r.height}};
        }})()
        """
        eval_result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
            label="Runtime.evaluate",
        )

        rect = eval_result.get("result", {}).get("value")
        if rect is None:
            return make_error(f"E006: No element found matching selector '{selector}'")

        if rect.get("width", 0) == 0 or rect.get("height", 0) == 0:
            return make_error(
                f"Element '{selector}' has zero dimensions (possibly hidden or off-screen)"
            )

        clip = {
            "x": rect["x"],
            "y": rect["y"],
            "width": rect["width"],
            "height": rect["height"],
            "scale": 1,
        }

        shot_result = await _cdp_call(
            cdp,
            "Page.captureScreenshot",
            {"format": "png", "clip": clip},
            label="Page.captureScreenshot",
        )

        img_data = shot_result.get("data", "")
        if not img_data:
            return make_error("No image data returned from Chrome")

        out_path = arguments.get("path", "")
        lines = []
        if out_path:
            try:
                img_bytes = base64.b64decode(img_data)
                abs_path = str(Path(out_path).resolve())
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                Path(abs_path).write_bytes(img_bytes)
                lines.append(f"Screenshot saved to: {abs_path}")
            except Exception as exc:
                logger.warning("Could not write screenshot to %s: %s", out_path, exc)
                lines.append(f"Warning: could not write file: {exc}")

        lines.append(f"data:image/png;base64,{img_data}")
        return make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Screencast capture (catches transient states like loading spinners) #
    # ------------------------------------------------------------------ #

    async def _handle_screencast_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Begin screencast capture; delegates to :class:`ScreencastRecorder`."""
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err
        return await self._screencast.start(cdp, arguments)

    async def _handle_screencast_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Stop screencast capture and write frames; delegates to the recorder."""
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err
        return await self._screencast.stop(cdp, arguments)

    # ------------------------------------------------------------------ #
    # Semantic wait tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_wait_idle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Wait for network to be idle (no new resources loaded for idle_ms).

        Uses PerformanceResourceTiming to detect in-flight activity.
        Does not subscribe to Network CDP events -- avoids the two-CDP-client
        constraint (D-006).

        Args:
            arguments: Tool arguments with optional 'timeout_ms' and 'idle_ms'.

        Returns:
            JSON-RPC style response dict.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        timeout_ms = int(arguments.get("timeout_ms", 5000))
        idle_ms = int(arguments.get("idle_ms", 500))

        # JS: poll until resource count is stable for idle_ms
        js = f"""
        new Promise((resolve, reject) => {{
            const IDLE_MS = {idle_ms};
            const TIMEOUT_MS = {timeout_ms};
            const POLL_MS = 100;
            const start = Date.now();
            let lastCount = performance.getEntriesByType('resource').length;
            let stableSince = Date.now();

            function check() {{
                const now = Date.now();
                if (now - start > TIMEOUT_MS) {{
                    reject(new Error('E008: wait_idle timed out after ' + TIMEOUT_MS + 'ms'));
                    return;
                }}
                const count = performance.getEntriesByType('resource').length;
                if (count !== lastCount) {{
                    lastCount = count;
                    stableSince = now;
                }}
                if (now - stableSince >= IDLE_MS) {{
                    resolve('idle after ' + (now - start) + 'ms');
                    return;
                }}
                setTimeout(check, POLL_MS);
            }}
            setTimeout(check, POLL_MS);
        }})
        """

        result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout_ms + 2000,
            },
            label="wait_idle",
        )

        # Check for JS exception
        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_idle failed")
            return make_error(msg)

        msg = result.get("result", {}).get("value", "idle")
        return make_text(f"Network idle: {msg}")

    async def _handle_wait_stable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Wait for DOM mutations to stop for stable_ms.

        Injects a MutationObserver that resolves when no mutations occur
        for the quiescence period. Cleans up on both success and timeout.

        Args:
            arguments: Tool arguments with optional 'timeout_ms' and 'stable_ms'.

        Returns:
            JSON-RPC style response dict.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        timeout_ms = int(arguments.get("timeout_ms", 5000))
        stable_ms = int(arguments.get("stable_ms", 300))

        js = f"""
        new Promise((resolve, reject) => {{
            const STABLE_MS = {stable_ms};
            const TIMEOUT_MS = {timeout_ms};
            const start = Date.now();
            let timer = null;
            let observer = null;

            function cleanup() {{
                if (observer) {{ observer.disconnect(); observer = null; }}
                if (timer) {{ clearTimeout(timer); timer = null; }}
            }}

            const timeout = setTimeout(() => {{
                cleanup();
                reject(new Error('E008: wait_stable timed out after ' + TIMEOUT_MS + 'ms'));
            }}, TIMEOUT_MS);

            function resetTimer() {{
                if (timer) clearTimeout(timer);
                timer = setTimeout(() => {{
                    clearTimeout(timeout);
                    cleanup();
                    resolve('stable after ' + (Date.now() - start) + 'ms');
                }}, STABLE_MS);
            }}

            observer = new MutationObserver(resetTimer);
            observer.observe(document.documentElement, {{
                subtree: true,
                childList: true,
                attributes: true,
                characterData: true,
            }});

            // Start the initial stability timer
            resetTimer();
        }})
        """

        result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout_ms + 2000,
            },
            label="wait_stable",
        )

        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_stable failed")
            return make_error(msg)

        msg = result.get("result", {}).get("value", "stable")
        return make_text(f"DOM stable: {msg}")

    # ------------------------------------------------------------------ #
    # Content extraction tools                                             #
    # ------------------------------------------------------------------ #

    async def _handle_get_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get text content of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element text content.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        res = await eval_on_element(cdp, selector, "el.textContent", label="get_text")
        if not res.found:
            return make_error(f"E006: No element found matching selector '{selector}'")
        return make_text("" if res.value is None else str(res.value))

    async def _handle_get_html(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get outer HTML of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element outer HTML.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        res = await eval_on_element(cdp, selector, "el.outerHTML", label="get_html")
        if not res.found:
            return make_error(f"E006: No element found matching selector '{selector}'")
        return make_text("" if res.value is None else str(res.value))

    async def _handle_get_attr(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get attribute value of element by CSS selector.

        Returns null (not an error) if element exists but attribute is absent:
        a present element with an absent attribute yields a null value through
        :func:`eval_on_element`, distinct from the ``found=False`` an unmatched
        selector produces, so the two cases no longer need sentinel strings.

        Args:
            arguments: Tool arguments with 'selector' and 'attribute' keys.

        Returns:
            JSON-RPC style response dict with attribute value.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        attribute = arguments.get("attribute", "").strip()
        if not selector:
            return make_error("selector is required")
        if not attribute:
            return make_error("attribute is required")

        res = await eval_on_element(
            cdp, selector, f"el.getAttribute({attribute!r})", label="get_attr"
        )
        if not res.found:
            return make_error(f"E006: No element found matching selector '{selector}'")
        if res.value is None:
            return make_text(f"null (element exists but '{attribute}' attribute is not present)")
        return make_text(str(res.value))

    # ------------------------------------------------------------------ #
    # Element query tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_element_exists(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check if one or more elements matching a CSS selector exist.

        Never throws on not-found -- this is a boolean query.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with exists bool and count.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        js = f"document.querySelectorAll({selector!r}).length"
        result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
            label="element_exists",
        )

        # querySelectorAll throws for invalid selectors -- check for exception
        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "Invalid selector")
            return make_error(f"Invalid selector '{selector}': {msg}")

        count = result.get("result", {}).get("value", 0)
        exists = count > 0
        return make_text(f'{{"exists": {str(exists).lower()}, "count": {count}}}')

    async def _handle_element_visible(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check if an element is visible (rendered, non-zero size, not CSS-hidden).

        Visibility: element exists AND has non-zero bounding rect AND
        display != none AND visibility != hidden AND opacity > 0. A selector
        that matches nothing is reported as not visible rather than an error,
        so the not-found contract from :func:`eval_on_element` maps cleanly to
        ``visible=False``.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with visible bool.
        """
        cdp, err = self._cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        res = await eval_on_element(cdp, selector, _VISIBILITY_EXPR, label="element_visible")
        visible = res.found and bool(res.value)
        return make_text(f'{{"visible": {str(visible).lower()}}}')

    async def _refresh_frame_tree(self) -> None:
        """Refresh the frame tree from Chrome."""
        if self._cdp_client and self._cdp_client.connected:
            try:
                result = await self._cdp_client.send("Page.getFrameTree")
                if "frameTree" in result:
                    self._frame_manager.update_from_frame_tree(result["frameTree"])
            except Exception:
                logger.debug("Failed to refresh frame tree", exc_info=True)
