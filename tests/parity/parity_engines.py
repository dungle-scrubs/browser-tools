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

    def evaluate(self, script: str) -> Any:  # pragma: no cover - protocol
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

        ax_result = self._session.get_full_ax_tree()
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


def capture_corpus(engine: ParityEngine, pages: tuple[CorpusPage, ...] = CORPUS) -> dict[str, PageCapture]:
    """Run every corpus page through ``engine`` and collect the captures."""
    return {page.page_id: engine.capture(page) for page in pages}
