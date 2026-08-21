"""Native UID interaction on the CDP DOM/Input/Runtime domains.

RFC-01, "Native snapshot" (Phase 2, ticket #40). This module rebuilds the
``click --uid`` / ``fill --uid`` *interaction* path directly on Chrome
DevTools Protocol, with no Node subprocess. It is the interactive half of the
snapshot rebuild #39 began: #39 produces the accessibility tree and the stable
UID -> backend-node binding; this module resolves a UID against that binding
and dispatches the interaction over CDP.

The path is additive. The frozen MCP ``click`` / ``fill`` tools keep their
name, argument, and response shape; this backend is built beside the Node
path, and the authoritative flip plus the full parity gate are ticket #41.

Reuse of #39
------------
UID -> backend-node resolution is not reimplemented here. An interaction
resolves a UID through :class:`~browser_tools.native_snapshot.NativeSnapshotReader`
-- the same reader, the same current-snapshot-only stability contract -- and
reads :attr:`~browser_tools.native_snapshot.AxUidNode.backend_node_id`. A UID
from a superseded snapshot, or one naming a node with no DOM backing, is
rejected before any CDP call is dispatched.

Transport independence (sans-IO)
--------------------------------
The interaction *protocol* -- which CDP calls to make, in what order, and how
to use each result -- is expressed as a generator (:func:`click_steps`,
:func:`fill_steps`) that yields :class:`CdpCall` values and is fed each call's
result back. It performs no IO. Two thin drivers feed it:

- :func:`drive_sync` for a synchronous ``send`` (the parity harness's live
  Playwright CDP session, and unit tests' fake send).
- :func:`drive_async` for an async ``send`` (a bound
  :meth:`~browser_tools.cdp_client.CDPClient.send`, the shape the real tool
  will wire in #41).

Both drivers run the *same* protocol, so the CDP sequence is defined once and
is unit-testable by stepping the generator with recorded results, no browser.

CDP method choice (parity with the Node engine)
-----------------------------------------------
The Node engine (chrome-devtools-mcp, driving puppeteer) is the baseline the
#41 gate compares against. The native dispatch reproduces its semantics:

- ``click`` dispatches a **trusted mouse click at the element's clickable
  point**: ``DOM.getBoxModel`` to read the content-box quad, then
  ``Input.dispatchMouseEvent`` move/press/release at its centre. This mirrors
  puppeteer's ``ElementHandle.click``, which computes the clickable point and
  dispatches real mouse events through the Input domain. The alternative --
  ``DOM.resolveNode`` + ``Runtime.callFunctionOn`` ``el.click()`` -- fires an
  *untrusted* synthetic click that bypasses hit-testing and the compositor, so
  it would diverge from the baseline on overlays and default-action behaviour.
  Trusted mouse events are the parity-preserving choice for ``click``.

- ``fill`` **focuses the element then replaces its value in one shot**,
  firing ``input`` and ``change``: ``DOM.focus`` then ``DOM.resolveNode`` +
  ``Runtime.callFunctionOn`` running a value-set function through the native
  ``HTMLInputElement`` / ``HTMLTextAreaElement`` ``value`` setter. This mirrors
  puppeteer/Playwright ``fill``, which clears and sets the value and dispatches
  ``input`` (not one event per keystroke -- that is ``type`` /
  ``pressSequentially``). ``Input.insertText`` was the alternative, but it
  appends at the caret (needing a separate clear), composes text as if typed,
  and does not fire ``change``; the value-set path matches ``fill`` semantics
  more precisely and deterministically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .native_snapshot import AxUidNode, NativeSnapshotReader

# CDP methods this interaction path uses.
DOM_SCROLL_INTO_VIEW = "DOM.scrollIntoViewIfNeeded"
DOM_GET_BOX_MODEL = "DOM.getBoxModel"
DOM_FOCUS = "DOM.focus"
DOM_RESOLVE_NODE = "DOM.resolveNode"
INPUT_DISPATCH_MOUSE = "Input.dispatchMouseEvent"
RUNTIME_CALL_FUNCTION_ON = "Runtime.callFunctionOn"

# Synchronous / asynchronous CDP transports: ``send(method, params) -> result``.
SyncSend = Callable[..., dict[str, Any]]
AsyncSend = Callable[..., Awaitable[dict[str, Any]]]

Action = Literal["click", "fill"]

# In-page value setter for ``fill``. Sets the value through the native element
# ``value`` setter (so framework-tracked inputs see the change) and dispatches
# the ``input`` and ``change`` events a real fill produces. Returns the value
# actually set, for the caller to confirm.
_FILL_FUNCTION = """
function(value) {
  const el = this;
  let proto = null;
  if (el instanceof HTMLTextAreaElement) proto = HTMLTextAreaElement.prototype;
  else if (el instanceof HTMLInputElement) proto = HTMLInputElement.prototype;
  if (proto) {
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
  } else if (el.isContentEditable) {
    el.textContent = value;
  } else {
    el.value = value;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return el.value !== undefined ? el.value : el.textContent;
}
"""


class UidResolutionError(Exception):
    """A UID could not be resolved to an actionable backend DOM node.

    Raised before any CDP call is dispatched, for a UID that does not resolve
    in the current snapshot (stale, unknown, or superseded by a newer snapshot
    or a navigation) or that resolves to an accessibility node with no
    ``backendDOMNodeId`` (nothing in the DOM to interact with).
    """

    def __init__(self, uid: str, reason: str) -> None:
        self.uid = uid
        self.reason = reason
        super().__init__(f"cannot interact with uid {uid!r}: {reason}")


@dataclass(frozen=True)
class CdpCall:
    """One CDP method call the interaction protocol asks a driver to make."""

    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class InteractionResult:
    """The outcome of one native interaction, for confirmation and debugging.

    Attributes:
        action: ``"click"`` or ``"fill"``.
        uid: The UID that was resolved.
        backend_node_id: The DOM ``backendDOMNodeId`` the UID resolved to.
        methods: The CDP methods dispatched, in order. Lets a unit test pin the
            exact call sequence without a browser.
        point: For ``click``, the ``(x, y)`` viewport point clicked; else None.
        text: For ``fill``, the text requested; else None.
        value_after: For ``fill``, the element value after the set (as the
            in-page function reported it); else None.
    """

    action: Action
    uid: str
    backend_node_id: int
    methods: tuple[str, ...]
    point: tuple[float, float] | None = None
    text: str | None = None
    value_after: str | None = None


def _box_centre(box_model: dict[str, Any]) -> tuple[float, float]:
    """Centre of a ``DOM.getBoxModel`` content quad.

    ``model.content`` is a flat 8-number quad ``[x1,y1,x2,y2,x3,y3,x4,y4]``.
    The centre is the mean of the four corners -- the clickable point puppeteer
    also targets.
    """
    model = box_model.get("model", {})
    quad = model.get("content")
    if not isinstance(quad, list) or len(quad) < 8:
        raise ValueError("DOM.getBoxModel returned no usable content quad")
    xs = quad[0:8:2]
    ys = quad[1:8:2]
    return (sum(xs) / 4.0, sum(ys) / 4.0)


def click_steps(node: AxUidNode) -> Generator[CdpCall, dict[str, Any], InteractionResult]:
    """Sans-IO protocol for a native click on a resolved node.

    Yields the CDP calls a driver must dispatch and consumes each result;
    returns the :class:`InteractionResult`. The node MUST have a backend node
    (the resolver guarantees this).
    """
    backend = node.backend_node_id
    assert backend is not None  # guaranteed by the resolver
    methods: list[str] = []

    methods.append(DOM_SCROLL_INTO_VIEW)
    yield CdpCall(DOM_SCROLL_INTO_VIEW, {"backendNodeId": backend})

    methods.append(DOM_GET_BOX_MODEL)
    box = yield CdpCall(DOM_GET_BOX_MODEL, {"backendNodeId": backend})
    x, y = _box_centre(box)

    for event_type, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
        methods.append(INPUT_DISPATCH_MOUSE)
        params: dict[str, Any] = {"type": event_type, "x": x, "y": y}
        if event_type != "mouseMoved":
            params.update({"button": "left", "clickCount": 1})
        params["buttons"] = buttons
        yield CdpCall(INPUT_DISPATCH_MOUSE, params)

    return InteractionResult(
        action="click",
        uid=node.uid,
        backend_node_id=backend,
        methods=tuple(methods),
        point=(x, y),
    )


def fill_steps(
    node: AxUidNode, text: str
) -> Generator[CdpCall, dict[str, Any], InteractionResult]:
    """Sans-IO protocol for a native fill on a resolved node."""
    backend = node.backend_node_id
    assert backend is not None  # guaranteed by the resolver
    methods: list[str] = []

    methods.append(DOM_SCROLL_INTO_VIEW)
    yield CdpCall(DOM_SCROLL_INTO_VIEW, {"backendNodeId": backend})

    methods.append(DOM_FOCUS)
    yield CdpCall(DOM_FOCUS, {"backendNodeId": backend})

    methods.append(DOM_RESOLVE_NODE)
    resolved = yield CdpCall(DOM_RESOLVE_NODE, {"backendNodeId": backend})
    object_id = resolved.get("object", {}).get("objectId")
    if not object_id:
        raise ValueError("DOM.resolveNode returned no objectId for the fill target")

    methods.append(RUNTIME_CALL_FUNCTION_ON)
    call_result = yield CdpCall(
        RUNTIME_CALL_FUNCTION_ON,
        {
            "objectId": object_id,
            "functionDeclaration": _FILL_FUNCTION,
            "arguments": [{"value": text}],
            "returnByValue": True,
        },
    )
    value_after = call_result.get("result", {}).get("value")

    return InteractionResult(
        action="fill",
        uid=node.uid,
        backend_node_id=backend,
        methods=tuple(methods),
        text=text,
        value_after=str(value_after) if value_after is not None else None,
    )


def drive_sync(
    steps: Generator[CdpCall, dict[str, Any], InteractionResult],
    send: SyncSend,
) -> InteractionResult:
    """Run an interaction protocol against a synchronous ``send``."""
    try:
        call = next(steps)
        while True:
            result = send(call.method, call.params)
            call = steps.send(result if isinstance(result, dict) else {})
    except StopIteration as stop:
        return stop.value  # type: ignore[no-any-return]


async def drive_async(
    steps: Generator[CdpCall, dict[str, Any], InteractionResult],
    send: AsyncSend,
) -> InteractionResult:
    """Run an interaction protocol against an async ``send``."""
    try:
        call = next(steps)
        while True:
            result = await send(call.method, call.params)
            call = steps.send(result if isinstance(result, dict) else {})
    except StopIteration as stop:
        return stop.value  # type: ignore[no-any-return]


@dataclass
class NativeInteractor:
    """Resolve UIDs through #39's reader and dispatch native interactions.

    One interactor pairs with one :class:`NativeSnapshotReader` (one page /
    session). Resolution reuses the reader's current-snapshot-only contract, so
    an interaction is refused for exactly the UIDs ``snapshot`` would no longer
    resolve.
    """

    reader: NativeSnapshotReader
    _last_methods: tuple[str, ...] = field(default=(), repr=False)

    def resolve(self, uid: str) -> AxUidNode:
        """Resolve a UID to an actionable node, or raise :class:`UidResolutionError`."""
        node = self.reader.resolve_uid(uid)
        if node is None:
            if self.reader.current is None:
                reason = "no current snapshot (take a snapshot first)"
            else:
                reason = "not in the current snapshot (stale after a newer snapshot or navigation)"
            raise UidResolutionError(uid, reason)
        if node.backend_node_id is None:
            raise UidResolutionError(uid, "node has no backend DOM node to interact with")
        return node

    # -- synchronous drivers (parity harness, tests) ----------------------- #

    def click(self, send: SyncSend, uid: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native click over a sync transport."""
        return drive_sync(click_steps(self.resolve(uid)), send)

    def fill(self, send: SyncSend, uid: str, text: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native fill over a sync transport."""
        return drive_sync(fill_steps(self.resolve(uid), text), send)

    # -- asynchronous drivers (real CDPClient; wired in #41) --------------- #

    async def click_async(self, send: AsyncSend, uid: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native click over an async transport."""
        return await drive_async(click_steps(self.resolve(uid)), send)

    async def fill_async(self, send: AsyncSend, uid: str, text: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native fill over an async transport."""
        return await drive_async(fill_steps(self.resolve(uid), text), send)


__all__ = [
    "CdpCall",
    "InteractionResult",
    "NativeInteractor",
    "UidResolutionError",
    "click_steps",
    "drive_async",
    "drive_sync",
    "fill_steps",
]
