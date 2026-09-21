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

Resolution without a snapshot
-----------------------------
A UID is ``"<docToken>-<backendNodeId>"``, so it carries everything an
interaction needs: the document it belongs to, and the DOM node it names. An
interaction reads the live document token from ``Page.getFrameTree`` and
compares it. Matching means the UID belongs to the document on screen; not
matching means the page navigated, and the UID is refused with the remedy.

No snapshot is taken and none is needed. ``click`` and ``fill`` used to take
one internally, which is what made the old staleness check unable to fire: the
fresh snapshot was always "current", so a UID minted against a different tree
resolved against it by ordinal and named whatever node now sat at that
position. Dropping the internal snapshot and carrying the backend node in the
UID are one change, not two -- without the second, a one-shot process would
have no current snapshot and every UID would fail to resolve.

The document check runs before any interaction traffic. The element check runs
first inside the protocol: ``DOM.describeNode`` confirms the backend node is an
Element, because focusing or clicking a text node is how the old scheme's
wrong-target failures surfaced when they surfaced at all.

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

from .native_snapshot import parse_uid, read_live_doc_tokens, read_live_doc_tokens_sync

if TYPE_CHECKING:
    from .native_snapshot import NativeSnapshotReader

# CDP methods this interaction path uses.
DOM_DESCRIBE_NODE = "DOM.describeNode"
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

    Raised before any interaction call is dispatched, for a UID minted against
    a different document (the page navigated), one that names no DOM node, or
    one whose node is not an Element.
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


#: DOM ``nodeType`` for an element. A UID naming anything else -- a text node
#: most often -- is refused rather than focused or clicked.
ELEMENT_NODE_TYPE = 1


def _require_element(
    uid: str, backend: int, methods: list[str]
) -> Generator[CdpCall, dict[str, Any]]:
    """Confirm the backend node is an Element before acting on it."""
    methods.append(DOM_DESCRIBE_NODE)
    described = yield CdpCall(DOM_DESCRIBE_NODE, {"backendNodeId": backend})
    node_type = described.get("node", {}).get("nodeType")
    if node_type != ELEMENT_NODE_TYPE:
        raise UidResolutionError(
            uid,
            f"names a DOM node that is not an element (nodeType {node_type}); "
            "take a new snapshot and use the element's own uid",
        )


def click_steps(uid: str, backend: int) -> Generator[CdpCall, dict[str, Any], InteractionResult]:
    """Sans-IO protocol for a native click on ``backend``.

    Yields the CDP calls a driver must dispatch and consumes each result;
    returns the :class:`InteractionResult`.
    """
    methods: list[str] = []

    yield from _require_element(uid, backend, methods)

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
        uid=uid,
        backend_node_id=backend,
        methods=tuple(methods),
        point=(x, y),
    )


def fill_steps(
    uid: str, backend: int, text: str
) -> Generator[CdpCall, dict[str, Any], InteractionResult]:
    """Sans-IO protocol for a native fill on ``backend``."""
    methods: list[str] = []

    yield from _require_element(uid, backend, methods)

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
        uid=uid,
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
            call = steps.send(result)
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
            call = steps.send(result)
    except StopIteration as stop:
        return stop.value  # type: ignore[no-any-return]


@dataclass
class NativeInteractor:
    """Dispatch native interactions for UIDs minted by #39's reader.

    One interactor pairs with one :class:`NativeSnapshotReader` (one page /
    session), but it does not read the reader's snapshot to resolve a UID. A
    UID carries its document token and its backend DOM node, so resolution is a
    comparison against the live document plus a parse -- which is what lets
    ``click`` and ``fill`` act without taking a snapshot of their own.

    The reader is kept for the snapshot path and for diagnostics.
    """

    reader: NativeSnapshotReader
    _last_methods: tuple[str, ...] = field(default=(), repr=False)

    def _backend_for(self, uid: str, live_tokens: set[str]) -> int:
        """Check ``uid`` against the live documents and return its backend node.

        Against all of them, not against the main frame's. A UID minted
        inside an iframe carries that iframe's document token, and a page
        holds as many live documents as it has frames. Checking one token
        refused a node that was on the screen.

        Raises:
            UidResolutionError: The UID belongs to no live document, or
                names no DOM node.
        """
        doc_token, backend = parse_uid(uid)
        if doc_token not in live_tokens:
            raise UidResolutionError(
                uid,
                "minted against a previous document (the page navigated since); "
                "take a new snapshot",
            )
        if backend is None:
            raise UidResolutionError(uid, "names a node with no backend DOM node")
        return backend

    def resolve(self, send: SyncSend, uid: str) -> int:
        """Backend DOM node ``uid`` names, checked against the live documents."""
        return self._backend_for(uid, read_live_doc_tokens_sync(send))

    async def resolve_async(self, send: AsyncSend, uid: str) -> int:
        """Awaitable :meth:`resolve`."""
        return self._backend_for(uid, await read_live_doc_tokens(send))

    # -- synchronous drivers (parity harness, tests) ----------------------- #

    def click(self, send: SyncSend, uid: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native click over a sync transport."""
        return drive_sync(click_steps(uid, self.resolve(send, uid)), send)

    def fill(self, send: SyncSend, uid: str, text: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native fill over a sync transport."""
        return drive_sync(fill_steps(uid, self.resolve(send, uid), text), send)

    # -- asynchronous drivers (the real CDPClient) ------------------------- #

    async def click_async(self, send: AsyncSend, uid: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native click over an async transport."""
        backend = await self.resolve_async(send, uid)
        return await drive_async(click_steps(uid, backend), send)

    async def fill_async(self, send: AsyncSend, uid: str, text: str) -> InteractionResult:
        """Resolve ``uid`` and dispatch a native fill over an async transport."""
        backend = await self.resolve_async(send, uid)
        return await drive_async(fill_steps(uid, backend, text), send)


__all__ = [
    "ELEMENT_NODE_TYPE",
    "CdpCall",
    "InteractionResult",
    "NativeInteractor",
    "UidResolutionError",
    "click_steps",
    "drive_async",
    "drive_sync",
    "fill_steps",
]
