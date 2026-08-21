"""Native accessibility snapshot read path on the CDP Accessibility domain.

RFC-01, "Native snapshot" (Phase 2, ticket #39). This module rebuilds the
snapshot *read* path directly on Chrome DevTools Protocol's Accessibility
domain, with no Node subprocess. It replaces the chrome-devtools-mcp snapshot
for reading a page's accessibility tree and assigning stable UIDs to nodes.

UID interaction (``click --uid`` / ``fill --uid``) is ticket #40 and is NOT
built here; this module only produces the tree and the UID -> node binding a
later interaction step resolves against.

Scope boundary
--------------
The module is deliberately free of any browser, WebSocket, or transport
dependency. Its input is the raw ``Accessibility.getFullAXTree`` result dict
(``{"nodes": [...]}``); its async entry point takes a ``send`` callable so the
same code drives a live :class:`~browser_tools.cdp_client.CDPClient` or a
recorded fixture. That keeps the UID assignment and stability scheme unit
testable against synthetic / recorded CDP responses with no live browser
(RFC-01 Testing Strategy; ticket #39).

UID assignment and stability scheme
-----------------------------------
A UID is ``"<generation>-<ordinal>"``:

- ``generation`` is a counter on the reader that increments on every snapshot
  and on every recorded navigation. It stamps each UID with the snapshot it
  belongs to, so a UID minted by an earlier snapshot is recognizably stale and
  never silently resolves against a newer tree.
- ``ordinal`` is the node's 1-based position in a depth-first walk of the tree
  from the root through ``childIds``. The walk is deterministic, so the same
  accessibility tree always yields the same ordinal for the same node: two
  reads of one unchanged page assign matching ordinals (only the generation
  prefix differs).

The stability guarantee the RFC fixes -- *a UID returned by ``snapshot``
resolves to the same node for subsequent ``click --uid`` / ``fill --uid`` calls
until the next snapshot or navigation* -- is enforced by
:class:`NativeSnapshotReader`:

- A snapshot (:class:`NativeSnapshot`) is immutable, so resolving a UID against
  it always returns the same node for the snapshot's lifetime.
- The reader resolves UIDs only against its *current* snapshot. Taking a new
  snapshot, or recording a navigation, supersedes the current one; UIDs from
  any superseded snapshot no longer resolve.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# CDP methods this read path uses.
AX_ENABLE = "Accessibility.enable"
AX_GET_FULL_TREE = "Accessibility.getFullAXTree"
# Cross-frame stitching (see ``stitch_ax_frames``): discover child frames and
# the DOM node that owns each, so a child frame's accessibility tree can be
# spliced under its ``Iframe`` node.
PAGE_ENABLE = "Page.enable"
PAGE_GET_FRAME_TREE = "Page.getFrameTree"
DOM_GET_FRAME_OWNER = "DOM.getFrameOwner"

# Async CDP transport: ``send(method, params) -> result``.
CdpSend = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AxUidNode:
    """One accessibility node with its snapshot-stable UID.

    Attributes:
        uid: Stable identifier of the form ``"<generation>-<ordinal>"``, valid
            for the lifetime of the snapshot that minted it.
        role: Computed ARIA role (``role.value`` from the AX node).
        name: Computed accessible name (``""`` when absent).
        value: Computed value string, or ``None`` when the node has no value.
        backend_node_id: The DOM ``backendDOMNodeId`` this node maps to, or
            ``None`` for nodes with no DOM backing. This is the node a UID
            interaction ultimately resolves to.
        ax_node_id: The raw CDP ``nodeId`` of the AX node (per-session, opaque).
        parent_uid: UID of the parent node, or ``None`` for the root.
        child_uids: UIDs of the child nodes, in document order.
        ignored: True when the node is excluded from the accessibility tree
            (``ignored`` in the AX node). Ignored nodes still receive a UID so
            the tree walk is total, but they are excluded from the node set.
        properties: Selected boolean/state AX properties (e.g. ``disabled``,
            ``checked``), name -> value.
    """

    uid: str
    role: str
    name: str
    value: str | None
    backend_node_id: int | None
    ax_node_id: str
    parent_uid: str | None
    child_uids: tuple[str, ...]
    ignored: bool
    properties: dict[str, Any] = field(default_factory=dict)


# AX properties carried onto the node (the interactive-state subset that
# ``ax_find`` already surfaces, kept identical so both paths report the same).
_CARRIED_PROPERTIES = frozenset(
    {"disabled", "checked", "expanded", "required", "selected", "focused"}
)


def _ax_string(field_value: Any) -> str | None:
    """Extract the ``.value`` string from an AX ``{type, value}`` field."""
    if not isinstance(field_value, dict):
        return None
    value = field_value.get("value")
    if value is None:
        return None
    return str(value)


def _carried_properties(raw_node: dict[str, Any]) -> dict[str, Any]:
    """Pull the interactive-state properties from a raw AX node."""
    props: dict[str, Any] = {}
    for prop in raw_node.get("properties", []) or []:
        name = prop.get("name", "")
        if name in _CARRIED_PROPERTIES:
            props[name] = prop.get("value", {}).get("value")
    return props


@dataclass(frozen=True)
class NativeSnapshot:
    """An immutable accessibility snapshot with stable UIDs.

    A snapshot is produced by :class:`NativeSnapshotReader`. Its UID -> node
    binding never changes for the object's lifetime, which is what makes a UID
    stable "until the next snapshot or navigation": the reader simply stops
    pointing at this object.
    """

    generation: int
    root_uid: str | None
    nodes: tuple[AxUidNode, ...]
    _by_uid: dict[str, AxUidNode] = field(default_factory=dict, repr=False)

    def resolve(self, uid: str) -> AxUidNode | None:
        """Return the node for ``uid`` within this snapshot, or ``None``."""
        return self._by_uid.get(uid)

    def backend_node_for(self, uid: str) -> int | None:
        """Return the ``backendDOMNodeId`` a UID resolves to, or ``None``."""
        node = self._by_uid.get(uid)
        return node.backend_node_id if node is not None else None

    def visible_nodes(self) -> tuple[AxUidNode, ...]:
        """Nodes that are part of the accessibility tree (``ignored`` false)."""
        return tuple(node for node in self.nodes if not node.ignored)

    def format_tree(self) -> str:
        """Render an indented, UID-tagged text view of the visible tree.

        The line shape mirrors chrome-devtools-mcp's textual snapshot
        (``[uid=..] role "name"``) so a later cutover of the frozen
        ``take_snapshot`` tool onto this backend keeps the response shape. This
        module does not wire that cutover (see the module docstring).
        """
        if self.root_uid is None:
            return "(empty accessibility tree)"

        lines: list[str] = []

        def walk(uid: str, depth: int) -> None:
            node = self._by_uid.get(uid)
            if node is None:
                return
            if not node.ignored:
                indent = "  " * depth
                name = f' "{node.name}"' if node.name else ""
                value = f" = {node.value!r}" if node.value else ""
                lines.append(f"{indent}[uid={node.uid}] {node.role}{name}{value}")
                depth += 1
            for child_uid in node.child_uids:
                walk(child_uid, depth)

        walk(self.root_uid, 0)
        return "\n".join(lines)


class NativeSnapshotReader:
    """Builds native snapshots and enforces UID stability across them.

    One reader owns the snapshot lifecycle for one page/session. It mints a new
    :class:`NativeSnapshot` per read, tracks the current one, and resolves UIDs
    only against that current snapshot -- so a UID stays valid until the next
    snapshot or a recorded navigation, and no longer.
    """

    def __init__(self) -> None:
        self._generation = 0
        self._current: NativeSnapshot | None = None

    @property
    def current(self) -> NativeSnapshot | None:
        """The snapshot UIDs currently resolve against, or ``None``."""
        return self._current

    @property
    def generation(self) -> int:
        """The generation stamped on the most recent snapshot / navigation."""
        return self._generation

    def build(self, ax_result: dict[str, Any]) -> NativeSnapshot:
        """Build a snapshot from a raw ``Accessibility.getFullAXTree`` result.

        Increments the generation, assigns UIDs in depth-first document order,
        and installs the result as the current snapshot (superseding any prior
        one). Pure with respect to CDP transport: callable with a recorded
        response dict in a unit test.

        Args:
            ax_result: The ``getFullAXTree`` result, ``{"nodes": [...]}``.

        Returns:
            The freshly built, current :class:`NativeSnapshot`.
        """
        self._generation += 1
        generation = self._generation
        raw_nodes: list[dict[str, Any]] = list(ax_result.get("nodes", []) or [])

        by_ax_id: dict[str, dict[str, Any]] = {}
        for raw in raw_nodes:
            ax_id = raw.get("nodeId")
            if ax_id is not None:
                by_ax_id[str(ax_id)] = raw

        root_ax_id = _find_root_ax_id(raw_nodes, by_ax_id)

        # Depth-first walk from the root, minting UIDs in document (pre-order)
        # order: a node's ordinal precedes its descendants'. A UID is minted for
        # every reachable node so the walk is total; the ignored flag is
        # preserved for node-set filtering. ``order`` records the pre-order
        # sequence so ``nodes`` is returned in document order even though a
        # node object is built only after its children (it carries child UIDs).
        order: list[str] = []
        by_uid: dict[str, AxUidNode] = {}
        ordinal = 0

        def make_uid() -> str:
            nonlocal ordinal
            ordinal += 1
            return f"{generation}-{ordinal}"

        def walk(ax_id: str, parent_uid: str | None) -> str | None:
            raw = by_ax_id.get(ax_id)
            if raw is None:
                return None
            uid = make_uid()
            order.append(uid)
            child_uids: list[str] = []
            for child_ax_id in raw.get("childIds", []) or []:
                child_uid = walk(str(child_ax_id), uid)
                if child_uid is not None:
                    child_uids.append(child_uid)
            backend = raw.get("backendDOMNodeId")
            by_uid[uid] = AxUidNode(
                uid=uid,
                role=_ax_string(raw.get("role")) or "",
                name=_ax_string(raw.get("name")) or "",
                value=_ax_string(raw.get("value")),
                backend_node_id=int(backend) if isinstance(backend, int) else None,
                ax_node_id=str(ax_id),
                parent_uid=parent_uid,
                child_uids=tuple(child_uids),
                ignored=bool(raw.get("ignored", False)),
                properties=_carried_properties(raw),
            )
            return uid

        root_uid = walk(root_ax_id, None) if root_ax_id is not None else None

        snapshot = NativeSnapshot(
            generation=generation,
            root_uid=root_uid,
            nodes=tuple(by_uid[uid] for uid in order),
            _by_uid=by_uid,
        )
        self._current = snapshot
        return snapshot

    async def snapshot(self, send: CdpSend) -> NativeSnapshot:
        """Enable the Accessibility domain, read the full tree, build a snapshot.

        Args:
            send: Async CDP transport, ``send(method, params) -> result``
                (a bound :meth:`~browser_tools.cdp_client.CDPClient.send`).

        Returns:
            The freshly built, current :class:`NativeSnapshot`.
        """
        await send(AX_ENABLE)
        result = await send(AX_GET_FULL_TREE)
        return self.build(result)

    async def snapshot_stitched(self, send: CdpSend) -> NativeSnapshot:
        """Read the full tree stitched across child frames, then build a snapshot.

        The cross-frame counterpart of :meth:`snapshot`: it reaches across iframe
        boundaries (see :func:`read_stitched_ax_tree`) so the node set matches the
        Node baseline's on framed pages. This is the read the flipped native
        backend uses for ``take_snapshot`` (ticket #41).

        Args:
            send: Async CDP transport, ``send(method, params) -> result``.

        Returns:
            The freshly built, current :class:`NativeSnapshot`.
        """
        return self.build(await read_stitched_ax_tree(send))

    def resolve_uid(self, uid: str) -> AxUidNode | None:
        """Resolve a UID against the current snapshot only.

        Returns ``None`` when there is no current snapshot or when ``uid``
        belongs to a superseded snapshot -- the stability contract in one place.
        """
        if self._current is None:
            return None
        return self._current.resolve(uid)

    def backend_node_for_uid(self, uid: str) -> int | None:
        """Backend DOM node a UID resolves to in the current snapshot."""
        node = self.resolve_uid(uid)
        return node.backend_node_id if node is not None else None

    def note_navigation(self) -> None:
        """Record a navigation: invalidate the current snapshot's UIDs.

        A navigation replaces the document, so every outstanding UID is stale.
        The generation is bumped so a snapshot taken after this navigation mints
        UIDs distinct from the pre-navigation ones even at the same ordinal.
        """
        self._current = None
        self._generation += 1


def stitch_ax_frames(
    top_result: dict[str, Any],
    child_frames: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    """Splice child-frame accessibility trees into the top frame's tree.

    ``Accessibility.getFullAXTree`` returns only the frame it is called on: the
    top-frame result carries an ``Iframe`` node for each child frame but not the
    child document's own nodes. chrome-devtools-mcp (the parity baseline) stitches
    child frames in; this reproduces that so the native node set reaches across a
    frame boundary (RFC-01 parity corpus's iframe case, ticket #41).

    The transform is pure -- it takes the already-fetched CDP results and returns
    one merged ``{"nodes": [...]}`` dict the existing :meth:`NativeSnapshotReader.build`
    consumes unchanged. It never touches CDP transport, so it is unit-testable on
    synthetic dicts.

    Two facts make the splice sound:

    - AX ``nodeId`` values are per-frame and collide across frames, so every child
      frame's ids (its ``nodeId``, ``parentId``, and ``childIds``) are namespaced
      with a per-frame prefix before merging.
    - A child frame's ``DOM.getFrameOwner`` ``backendNodeId`` equals the
      ``backendDOMNodeId`` of the top tree's ``Iframe`` node for that frame, so the
      child's root is linked as a child of that ``Iframe`` node.

    Args:
        top_result: The top frame's ``getFullAXTree`` result.
        child_frames: ``(owner_backend_node_id, child_ax_result)`` for each child
            frame, where ``owner_backend_node_id`` is the frame owner's DOM
            ``backendNodeId`` from ``DOM.getFrameOwner``.

    Returns:
        A merged ``{"nodes": [...]}`` with child frames spliced under their owners.
    """
    merged: list[dict[str, Any]] = [dict(node) for node in top_result.get("nodes", []) or []]
    backend_to_node: dict[int, dict[str, Any]] = {}
    for node in merged:
        backend = node.get("backendDOMNodeId")
        if isinstance(backend, int):
            backend_to_node[backend] = node

    for index, (owner_backend, child_result) in enumerate(child_frames):
        prefix = f"f{index + 1}:"
        child_root_id: str | None = None
        child_nodes: list[dict[str, Any]] = []
        for raw in child_result.get("nodes", []) or []:
            node = dict(raw)
            node["nodeId"] = prefix + str(node.get("nodeId"))
            parent_id = node.get("parentId")
            if parent_id is not None:
                node["parentId"] = prefix + str(parent_id)
            else:
                child_root_id = node["nodeId"]
            node["childIds"] = [prefix + str(cid) for cid in (node.get("childIds") or [])]
            child_nodes.append(node)

        owner = backend_to_node.get(owner_backend)
        if owner is not None and child_root_id is not None:
            owner["childIds"] = [*(owner.get("childIds") or []), child_root_id]
            for node in child_nodes:
                if node["nodeId"] == child_root_id:
                    node["parentId"] = owner["nodeId"]
        merged.extend(child_nodes)

    return {"nodes": merged}


def _iter_frame_ids(frame_tree_node: dict[str, Any]) -> list[str]:
    """Collect child (non-root) frame ids from a ``Page.getFrameTree`` node."""
    ids: list[str] = []

    def walk(node: dict[str, Any], is_root: bool) -> None:
        frame = node.get("frame", {})
        frame_id = frame.get("id")
        if not is_root and frame_id is not None:
            ids.append(str(frame_id))
        for child in node.get("childFrames", []) or []:
            walk(child, False)

    walk(frame_tree_node, True)
    return ids


async def read_stitched_ax_tree(send: CdpSend) -> dict[str, Any]:
    """Read the full accessibility tree, stitched across child frames.

    Enables the Accessibility and Page domains, reads the top frame's tree,
    discovers each child frame and its owner DOM node, reads each child frame's
    tree, and returns the single merged result (see :func:`stitch_ax_frames`).
    A frame whose owner or tree cannot be read is skipped rather than failing the
    whole snapshot, so a broken child frame degrades to the top-frame tree.

    Args:
        send: Async CDP transport, ``send(method, params) -> result``.

    Returns:
        A merged ``{"nodes": [...]}`` ready for :meth:`NativeSnapshotReader.build`.
    """
    await send(AX_ENABLE)
    top = await send(AX_GET_FULL_TREE)

    try:
        await send(PAGE_ENABLE)
        frame_tree = await send(PAGE_GET_FRAME_TREE)
    except Exception:
        return top
    frame_ids = _iter_frame_ids(frame_tree.get("frameTree", {}))
    if not frame_ids:
        return top

    child_frames: list[tuple[int, dict[str, Any]]] = []
    for frame_id in frame_ids:
        try:
            owner = await send(DOM_GET_FRAME_OWNER, {"frameId": frame_id})
            backend = owner.get("backendNodeId")
            if not isinstance(backend, int):
                continue
            child = await send(AX_GET_FULL_TREE, {"frameId": frame_id})
        except Exception:
            continue
        child_frames.append((backend, child))

    return stitch_ax_frames(top, child_frames)


def _find_root_ax_id(
    raw_nodes: list[dict[str, Any]],
    by_ax_id: dict[str, dict[str, Any]],
) -> str | None:
    """Find the root AX node id: the first node with no in-tree parent.

    ``getFullAXTree`` lists nodes in tree order with the root first, but each
    node also carries ``parentId``. The root is the node whose ``parentId`` is
    absent or points outside the returned set. Falls back to the first node.
    """
    for raw in raw_nodes:
        parent_id = raw.get("parentId")
        if parent_id is None or str(parent_id) not in by_ax_id:
            ax_id = raw.get("nodeId")
            return str(ax_id) if ax_id is not None else None
    if raw_nodes:
        ax_id = raw_nodes[0].get("nodeId")
        return str(ax_id) if ax_id is not None else None
    return None
