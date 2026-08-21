"""Engine adapters that turn a live browser into a ``PageCapture``.

An *engine* is anything that can run a corpus page and report a
:class:`~parity_comparison.PageCapture`. Phase 2 adds the native-snapshot
engine here and captures it as the candidate against a baseline taken from
another engine; the comparison operator (``parity_comparison``) does the rest.

This module ships one working engine, :class:`AriaSnapshotEngine`, which drives
any session exposing the ``call_tool`` interface (``CamoufoxSession`` in this
repo). Its snapshot input is Playwright's ARIA-snapshot YAML, a documented
format; :func:`parse_aria_snapshot` parses it and is unit-tested independently
of any browser. Only :meth:`AriaSnapshotEngine.capture` needs a live browser,
so unit tests exercise the parser and the operator while the live path stays
behind the ``parity`` marker / ``run_baseline`` entry point.

UID model for this engine: the ARIA path assigns no stable UID, so a UID is
modeled as the CSS id selector of an interactive element (``#email``) and its
target as a stable DOM-tree path for the element that selector resolves to.
Two engines that resolve the same UID to the same DOM path agree; the shared
DOM-path normalization is what makes the resolution comparable across
sessions. The native engine plugs in by normalizing its own UID -> backend
node the same way.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from parity_comparison import PageCapture, SnapshotNode
from parity_corpus import CORPUS, CorpusPage

from browser_tools.native_interaction import (
    DOM_RESOLVE_NODE,
    RUNTIME_CALL_FUNCTION_ON,
)
from browser_tools.native_snapshot import NativeSnapshotReader

# A line in Playwright ARIA-snapshot YAML:
#   <indent>- role "name" [attr=val]: value
# name, the attribute block, and the trailing value are each optional.
_ARIA_LINE = re.compile(
    r"""^\s*-\s+
        (?P<role>[A-Za-z][\w-]*)          # role token
        (?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?  # optional quoted accessible name
        (?P<attrs>(?:\s+\[[^\]]*\])*)?     # optional [attr] / [attr=val] blocks
        (?::\s*(?P<value>.*\S)?)?          # optional ": value"; trailing ":" alone is allowed
        \s*$""",
    re.VERBOSE,
)


def parse_aria_snapshot(tree: str) -> list[SnapshotNode]:
    """Parse Playwright ARIA-snapshot YAML into snapshot nodes.

    Property lines (``- /url: ...``) are skipped: they describe a node, they
    are not nodes. A trailing ``:`` with no value marks a node that has
    children and yields ``value=None``.

    Args:
        tree: The ARIA-snapshot text, one node per line.

    Returns:
        The nodes in document order. The operator ignores order, but keeping it
        makes captures readable and diffs stable.
    """
    nodes: list[SnapshotNode] = []
    for raw in tree.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        # Property lines start with "- /" (e.g. "- /url: ...") and are not nodes.
        if stripped.startswith("- /"):
            continue
        match = _ARIA_LINE.match(line)
        if match is None:
            continue
        role = match.group("role")
        name = match.group("name")
        value = match.group("value")
        if name is not None:
            name = name.replace('\\"', '"').replace("\\\\", "\\")
        nodes.append(SnapshotNode(role=role, name=name or "", value=value))
    return nodes


# JS that returns a stable DOM-tree path for one element. Used both to key and
# to resolve UID targets, so two engines normalize element identity the same way.
_DOM_PATH_FN = """
(el) => {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'html') {
    let part = node.tagName.toLowerCase();
    if (node.id) { part += '#' + node.id; parts.unshift(part); break; }
    const parent = node.parentNode;
    if (parent) {
      const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
      if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
    }
    parts.unshift(part);
    node = node.parentNode;
  }
  return parts.join('>');
}
"""

# JS that returns { "#id": domPath } for every interactive element with an id.
_UID_TARGETS_SCRIPT = f"""
(() => {{
  const domPath = {_DOM_PATH_FN};
  const out = {{}};
  const els = document.querySelectorAll('a[id], button[id], input[id], select[id], textarea[id]');
  for (const el of els) out['#' + el.id] = domPath(el);
  return out;
}})()
"""

# callFunctionOn body run with ``this`` bound to a resolved element: returns the
# element's id (or null) and its stable DOM path. Used by the native
# interaction engine to normalize a UID's *resolved backend node* to the same
# DOM path the JS UID-targets script computes, so the two are comparable.
# Same interactive tag set as ``_UID_TARGETS_SCRIPT`` so the resolved native
# targets are keyed and filtered identically to the baseline.
_INTERACTIVE_SELECTOR = "a[id], button[id], input[id], select[id], textarea[id]"

_BACKEND_TARGET_FN = f"""
function() {{
  const domPath = {_DOM_PATH_FN};
  const el = this;
  if (!el || el.nodeType !== 1) return null;
  const interactive = el.matches('{_INTERACTIVE_SELECTOR}');
  return {{ id: el.id || null, path: domPath(el), interactive: interactive }};
}}
"""

# JS that settles the page: waits for load, then a beat past the dynamic
# fixture's own deferred timeout, so post-paint content is present at capture.
_SETTLE_SCRIPT = """
new Promise((resolve) => {
  const done = () => setTimeout(() => resolve(true), 300);
  if (document.readyState === 'complete') done();
  else window.addEventListener('load', done, { once: true });
})
"""


@runtime_checkable
class ParityEngine(Protocol):
    """Anything that can capture a corpus page as a ``PageCapture``."""

    name: str

    def capture(self, page: CorpusPage) -> PageCapture:  # pragma: no cover - protocol
        ...


class ToolSession(Protocol):
    """The subset of a session the engine uses (``CamoufoxSession`` fits)."""

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class AriaSnapshotEngine:
    """Capture a corpus page via a Playwright ARIA-snapshot session.

    This is a live engine: :meth:`capture` navigates a real browser. Construct
    it with any object exposing ``call_tool`` (this repo's ``CamoufoxSession``).
    """

    name = "aria-snapshot"

    def __init__(self, session: ToolSession) -> None:
        self._session = session

    def _evaluate(self, script: str) -> Any:
        result = self._session.call_tool("evaluate", {"script": script})
        if "error" in result:
            raise RuntimeError(f"evaluate failed: {result['error']}")
        return result["result"]["value"]

    def capture(self, page: CorpusPage) -> PageCapture:
        """Navigate to ``page`` and capture its snapshot, UID targets, and text."""
        nav = self._session.call_tool("navigate", {"url": page.file_url()})
        if "error" in nav:
            raise RuntimeError(f"navigate failed: {nav['error']}")

        # Settle so dynamic / post-paint content is present before capture.
        self._evaluate(_SETTLE_SCRIPT)

        snap = self._session.call_tool("snapshot", {})
        if "error" in snap:
            raise RuntimeError(f"snapshot failed: {snap['error']}")
        tree = snap["result"]["tree"]
        nodes = tuple(parse_aria_snapshot(tree if isinstance(tree, str) else str(tree)))

        uid_targets = self._evaluate(_UID_TARGETS_SCRIPT) or {}
        text = self._evaluate("document.body.innerText")

        return PageCapture(
            page_id=page.page_id,
            url=page.file_url(),
            nodes=nodes,
            uid_targets={str(k): str(v) for k, v in dict(uid_targets).items()},
            text=str(text),
        )


class NativeCdpSession(Protocol):
    """The subset of a live Chromium+CDP session the native engine drives.

    A concrete adapter (``tests/parity/live_chromium.py``) drives Playwright
    Chromium: ``navigate`` does ``page.goto``, ``get_full_ax_tree`` sends
    ``Accessibility.getFullAXTree`` over a CDP session, ``evaluate`` runs JS.
    """

    def navigate(self, url: str) -> None:  # pragma: no cover - protocol
        ...

    def get_full_ax_tree(self) -> dict[str, Any]:  # pragma: no cover - protocol
        ...

    def get_stitched_ax_tree(self) -> dict[str, Any]:  # pragma: no cover - protocol
        ...

    def evaluate(self, script: str) -> Any:  # pragma: no cover - protocol
        ...

    def cdp_send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class NativeSnapshotEngine:
    """Capture a corpus page via the native CDP Accessibility-domain read path.

    This is the Phase 2 candidate engine (ticket #39). It builds the node set
    from :class:`~browser_tools.native_snapshot.NativeSnapshotReader` over a real
    ``Accessibility.getFullAXTree``, assigning the module's stable UIDs. It is a
    live engine: :meth:`capture` navigates a real Chromium via CDP.

    Text and UID targets are computed with the *same* JS the ARIA engine uses
    (``document.body.innerText`` and :data:`_UID_TARGETS_SCRIPT`), so those two
    parity dimensions are directly comparable across engines and the operator's
    signal is isolated to the snapshot node set -- exactly the surface the
    native rebuild changes. The node set carries each node's native UID and
    backend node for debugging; only ``(role, name, value)`` enters node-set
    equality (see ``parity_comparison``).
    """

    name = "native-cdp"

    def __init__(self, session: NativeCdpSession) -> None:
        self._session = session
        self._reader = NativeSnapshotReader()

    def capture(self, page: CorpusPage) -> PageCapture:
        """Navigate to ``page`` and capture its native snapshot, UIDs, and text."""
        self._session.navigate(page.file_url())
        # A navigation invalidates any prior snapshot's UIDs, matching the
        # module's stability contract before the fresh snapshot is taken.
        self._reader.note_navigation()

        # Settle so dynamic / post-paint content is present before capture.
        self._session.evaluate(_SETTLE_SCRIPT)

        ax_result = self._session.get_stitched_ax_tree()
        snapshot = self._reader.build(ax_result)
        nodes = tuple(
            SnapshotNode(
                role=node.role,
                name=node.name,
                value=node.value,
                uid=node.uid,
                backend_node=str(node.backend_node_id) if node.backend_node_id is not None else None,
            )
            for node in snapshot.visible_nodes()
        )

        uid_targets = self._session.evaluate(_UID_TARGETS_SCRIPT) or {}
        text = self._session.evaluate("document.body.innerText")

        return PageCapture(
            page_id=page.page_id,
            url=page.file_url(),
            nodes=nodes,
            uid_targets={str(k): str(v) for k, v in dict(uid_targets).items()},
            text=str(text),
        )


class NativeInteractionEngine:
    """Capture a corpus page's UID targets via the native *interaction* path.

    Ticket #40. Where :class:`NativeSnapshotEngine` computes ``uid_targets`` by
    a JS ``querySelectorAll`` (isolating the node-set signal for #39), this
    engine computes them the way ``click`` / ``fill`` actually resolve a UID:
    take each interactive accessibility node's stable native UID, read its
    backend DOM node (#39's binding), then resolve that backend node to its DOM
    element and path over CDP (``DOM.resolveNode`` + ``Runtime.callFunctionOn``)
    -- the same resolution the real interaction performs before dispatching.

    The result is keyed by the element's ``#id`` (as the ARIA/Node baseline is),
    so #41 can compare native UID resolution against the Node baseline under the
    operator's ``uid_target`` dimension: agreement means a native UID resolves
    to the same DOM node a Node-engine click/fill would.

    The node set and text are captured exactly as :class:`NativeSnapshotEngine`
    does, so this engine is a drop-in candidate for the whole operator.
    """

    name = "native-interaction"

    def __init__(self, session: NativeCdpSession) -> None:
        self._session = session
        self._reader = NativeSnapshotReader()

    def _resolve_backend_target(self, backend_node_id: int) -> dict[str, Any] | None:
        """Resolve a backend DOM node to ``{id, path}`` over CDP, or None."""
        resolved = self._session.cdp_send(DOM_RESOLVE_NODE, {"backendNodeId": backend_node_id})
        object_id = resolved.get("object", {}).get("objectId")
        if not object_id:
            return None
        call = self._session.cdp_send(
            RUNTIME_CALL_FUNCTION_ON,
            {"objectId": object_id, "functionDeclaration": _BACKEND_TARGET_FN, "returnByValue": True},
        )
        value = call.get("result", {}).get("value")
        return value if isinstance(value, dict) else None

    def capture(self, page: CorpusPage) -> PageCapture:
        """Navigate to ``page`` and capture its snapshot and native UID targets."""
        self._session.navigate(page.file_url())
        self._reader.note_navigation()
        self._session.evaluate(_SETTLE_SCRIPT)

        ax_result = self._session.get_stitched_ax_tree()
        snapshot = self._reader.build(ax_result)
        nodes = tuple(
            SnapshotNode(
                role=node.role,
                name=node.name,
                value=node.value,
                uid=node.uid,
                backend_node=str(node.backend_node_id) if node.backend_node_id is not None else None,
            )
            for node in snapshot.visible_nodes()
        )

        # UID resolution through the native path: native UID -> backend node ->
        # DOM element/path, keyed by the element id for cross-engine comparison.
        uid_targets: dict[str, str] = {}
        for node in snapshot.visible_nodes():
            if node.backend_node_id is None:
                continue
            target = self._resolve_backend_target(node.backend_node_id)
            if target and target.get("id") and target.get("interactive"):
                uid_targets["#" + str(target["id"])] = str(target["path"])

        text = self._session.evaluate("document.body.innerText")

        return PageCapture(
            page_id=page.page_id,
            url=page.file_url(),
            nodes=nodes,
            uid_targets=uid_targets,
            text=str(text),
        )


# --------------------------------------------------------------------------- #
# Node engine: the authoritative chrome-devtools-mcp baseline (ticket #41).
# --------------------------------------------------------------------------- #

# chrome-devtools-mcp renders its accessibility snapshot one node per line:
#   uid=1_4 textbox "Email" value="a@b" checked
# The role token follows ``uid=<uid>``; the accessible name is the first quoted
# string; ``value="..."`` (when present) is the node value.
_NODE_SNAPSHOT_LINE = re.compile(
    r'^\s*uid=(?P<uid>\S+)\s+(?P<role>\S+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$'
)


def node_engine_available() -> tuple[bool, str]:
    """Report whether the chrome-devtools-mcp Node engine can be launched here."""
    import shutil

    if shutil.which("npx") is None:
        return False, "npx (Node.js) is not on PATH"
    return True, ""


def parse_node_snapshot(text: str) -> list[SnapshotNode]:
    """Parse a chrome-devtools-mcp textual accessibility snapshot into nodes.

    Only lines carrying a ``uid=`` token are nodes; the header (``## Latest page
    snapshot``) and any trailing prose are skipped. ``(role, name, value)`` is
    extracted for the operator; the Node engine's own uid is not carried (UID
    resolution is compared through the shared JS, as for the ARIA engine).
    """
    nodes: list[SnapshotNode] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if "uid=" not in line:
            continue
        match = _NODE_SNAPSHOT_LINE.match(line)
        if match is None:
            continue
        role = match.group("role")
        name = match.group("name")
        if name is not None:
            name = name.replace('\\"', '"').replace("\\\\", "\\")
        value_match = re.search(r'\bvalue="((?:[^"\\]|\\.)*)"', match.group("rest") or "")
        value = value_match.group(1) if value_match else None
        nodes.append(SnapshotNode(role=role, name=name or "", value=value))
    return nodes


def _node_response_text(response: dict[str, Any]) -> str:
    """Join the text items of a chrome-devtools-mcp tool response."""
    content = response.get("result", {}).get("content", []) or []
    return "".join(item.get("text", "") for item in content if item.get("type") == "text")


def _extract_eval_value(response: dict[str, Any]) -> Any:
    """Extract the returned value from a chrome-devtools-mcp evaluate_script reply.

    chrome-devtools-mcp wraps a script result as ``Script ran on page and
    returned:`` followed by a fenced ```json`` block. Pull the JSON payload out
    and decode it so the shared-JS text / UID-target dimensions are directly
    comparable with the native and ARIA engines.
    """
    text = _node_response_text(response)
    fenced = re.search(r"```(?:json)?\s*\n(.*)\n```", text, re.DOTALL)
    payload = fenced.group(1) if fenced else text
    try:
        import json

        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


class NodeMcpSession:
    """A live chrome-devtools-mcp subprocess exposed to :class:`NodeEngine`.

    Use as a context manager so the subprocess (and the Chrome it launches) are
    always torn down::

        with NodeMcpSession() as node:
            engine = NodeEngine(node)

    It drives the real Node engine over the repo's :class:`McpBroker` -- the same
    JSON-RPC-over-stdio broker the production daemon uses -- so the parity gate
    compares native against chrome-devtools-mcp itself, not a stand-in.
    """

    _INIT_TIMEOUT = 120.0
    _CALL_TIMEOUT = 60.0

    def __init__(self, *, headless: bool = True, channel: str = "stable") -> None:
        self._headless = headless
        self._channel = channel
        self._broker: Any = None

    def __enter__(self) -> NodeMcpSession:
        from browser_tools.mcp_broker import McpBroker

        cmd = ["npx", "-y", "chrome-devtools-mcp@latest", "--isolated", "--channel", self._channel]
        if self._headless:
            cmd.append("--headless")
        self._broker = McpBroker(cmd)
        self._broker.start()
        init = self._broker.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "browser-tools-parity", "version": "1.0.0"},
            },
            timeout=self._INIT_TIMEOUT,
        )
        if "error" in init:
            raise RuntimeError(f"chrome-devtools-mcp initialize failed: {init['error']}")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._broker is not None:
            self._broker.terminate()

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._broker.request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout=self._CALL_TIMEOUT
        )
        if "error" in response:
            raise RuntimeError(f"chrome-devtools-mcp {tool} failed: {response['error']}")
        return response

    def navigate(self, url: str) -> None:
        self._call("navigate_page", {"type": "url", "url": url})

    def take_snapshot_text(self) -> str:
        return _node_response_text(self._call("take_snapshot", {}))

    def evaluate(self, expression: str) -> Any:
        """Run a JS expression via evaluate_script and return the decoded value."""
        function = f"() => ({expression})"
        return _extract_eval_value(self._call("evaluate_script", {"function": function}))

    def settle(self) -> None:
        """Await the shared settle script through the Node engine."""
        function = f"async () => {{ await {_SETTLE_SCRIPT.strip()}; return true; }}"
        self._call("evaluate_script", {"function": function})


class NodeEngine:
    """Capture a corpus page via the real chrome-devtools-mcp Node engine.

    This is the authoritative baseline the RFC-01 Phase 2 parity gate compares
    the native engine against (Testing Strategy, "Parity gate"). The node set is
    parsed from the Node engine's own ``take_snapshot`` output; text and UID
    targets are computed with the *same* shared JS the ARIA and native engines
    use, so the operator's signal is isolated to the snapshot node set.
    """

    name = "node-cdmcp"

    def __init__(self, session: NodeMcpSession) -> None:
        self._session = session

    def capture(self, page: CorpusPage) -> PageCapture:
        """Navigate to ``page`` and capture its Node snapshot, UIDs, and text."""
        self._session.navigate(page.file_url())
        self._session.settle()

        nodes = tuple(parse_node_snapshot(self._session.take_snapshot_text()))
        uid_targets = self._session.evaluate(_UID_TARGETS_SCRIPT.strip()) or {}
        text = self._session.evaluate("document.body.innerText")

        return PageCapture(
            page_id=page.page_id,
            url=page.file_url(),
            nodes=nodes,
            uid_targets={str(k): str(v) for k, v in dict(uid_targets).items()},
            text=str(text),
        )


def capture_corpus(engine: ParityEngine, pages: tuple[CorpusPage, ...] = CORPUS) -> dict[str, PageCapture]:
    """Run every corpus page through ``engine`` and collect the captures."""
    return {page.page_id: engine.capture(page) for page in pages}
