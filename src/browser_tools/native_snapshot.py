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
A UID is ``"<docToken>-<backendNodeId>"``:

- ``docToken`` names the *document* the UID was minted against: a digest of
  the frame id and the CDP ``loaderId``, both readable at any time from
  ``Page.getFrameTree``. The loaderId changes on every navigation, which
  makes the token the staleness guard - a UID whose token matches no live
  document is refused. The frame id is what lets two documents live in one
  snapshot, which is what merging a cross-origin iframe's tree requires.
- ``backendNodeId`` is the accessibility node's ``backendDOMNodeId`` -- the DOM
  node the UID names, which is also exactly what an interaction needs. Every
  addressable node already carries one.

**A UID is valid for the lifetime of the document. Taking another snapshot
invalidates nothing. Navigation is the only invalidation event.**

That is a correction, not a preference. The scheme this replaced was
``"<generation>-<ordinal>"``, where ``generation`` counted snapshots on the
reader and ``ordinal`` was a node's position in a depth-first walk. Every CLI
invocation builds a fresh reader whose counter starts at 1, and ``click`` and
``fill`` each took their own snapshot before acting, so a caller's UID was
resolved against a *different* tree carrying an identical ``1-`` prefix. An
ordinal that shifted between the two trees silently named a different node:
reproduced, ``1-3`` was a password textbox before a fill and a text node after
it. When a shifted ordinal lands on another element, the value goes into the
wrong field with no error at all.

The pair was chosen on measurement rather than argument. One handler drove
three tree reads with a fill between the second and third: the ordinal moved
for the password field, while ``backendDOMNodeId`` held at 3, 4 and 9 for the
three nodes across all three reads, including across the fill. A bare
``backendNodeId`` was rejected because it carries no document provenance, which
relocates the silent wrong-field failure into the next document.

A node with no ``backendDOMNodeId`` -- in practice only AX-internal leaves such
as ``InlineTextBox`` -- still needs a UID, because the tree walk and the
rendered output reference every node. Those get ``"<docToken>-x<ordinal>"``,
which cannot collide with a numeric backend id and is refused by the
interaction path with "no backend DOM node". The same form is used if two
accessibility nodes ever report the same ``backendDOMNodeId``: the first in
document order keeps the addressable UID. Across the whole parity corpus --
plain, form, iframe, shadow and dynamic pages -- no duplicate was observed and
only ``InlineTextBox`` nodes lacked a backend id.
"""

from __future__ import annotations

import hashlib
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

#: Hex characters of the document digest a docToken carries. 12 of them is
#: 48 bits, which keeps the UID short enough to print once per node and read
#: back by hand, against a population of the documents live in one browser.
DOC_TOKEN_CHARS = 12

#: Key stamped on a raw AX node naming the document it came from, so one
#: build can carry nodes from several documents. Read by :meth:`build`, which
#: falls back to the build's own token for an unstamped node.
NODE_DOC_TOKEN_KEY = "_btDocToken"

#: Prefix marking the ordinal fallback for a node with no backend DOM node.
#: A leading non-digit is what keeps it out of the numeric backend-id space.
NON_DOM_UID_PREFIX = "x"


def doc_token(frame_id: str, loader_id: str) -> str:
    """The identity of one document: the frame, and the load in it.

    A token must not be shared by two live documents. A UID carries it and
    nothing else, so the token is the only thing saying which document a
    UID's backend node id belongs to. Two properties the previous
    derivation lacked (RFC-04, Routing).

    *It was not an identity.* The old token was the first 12 characters of
    a 32-character ``loaderId``, so two loader ids differing only in the
    tail produced one token. A constructed collision rather than one Chrome
    is likely to produce, and the guarantee was still false.

    *A loaderId does not say which frame.* Merging a child frame's
    accessibility tree into the parent's puts two documents' backend node
    ids in one snapshot, and a backend node id is unique per document, not
    per page: the parent's node 2 and the child's node 2 are different
    nodes. The frame id is what keeps their UIDs apart.

    Hashed rather than concatenated, because a frame id is already 32
    characters and a UID is printed on every line of a snapshot.
    """
    digest = hashlib.sha256(f"{frame_id}\x00{loader_id}".encode()).hexdigest()
    return digest[:DOC_TOKEN_CHARS].upper()


def doc_token_from_frame(frame: dict[str, Any]) -> str:
    """The token for one ``frame`` object out of ``Page.getFrameTree``."""
    return doc_token(str(frame.get("id", "")), str(frame.get("loaderId", "")))


def make_uid(doc_token: str, backend_node_id: int) -> str:
    """Mint the UID naming ``backend_node_id`` within ``doc_token``'s document."""
    return f"{doc_token}-{backend_node_id}"


def parse_uid(uid: str) -> tuple[str, int | None]:
    """Split a UID into its document token and backend node id.

    Args:
        uid: A UID as ``snapshot`` printed it.

    Returns:
        ``(doc_token, backend_node_id)``. The backend node id is ``None`` when
        the UID names a node with no DOM backing (the ``x<ordinal>`` form) or
        when the UID is not in the expected shape at all -- a caller that needs
        to act refuses both the same way.
    """
    token, separator, rest = str(uid).partition("-")
    if not separator:
        return (token, None)
    return (token, int(rest)) if rest.isdigit() else (token, None)


async def read_doc_token(send: CdpSend) -> str:
    """Read the live document's token from ``Page.getFrameTree``.

    The main frame's ``loaderId`` changes on every navigation, which is what
    makes it the document's identity. Cheap: one CDP call, no accessibility
    tree.
    """
    result = await send(PAGE_GET_FRAME_TREE)
    return doc_token_from_frame(result.get("frameTree", {}).get("frame", {}))


def live_doc_tokens(frame_tree_node: dict[str, Any]) -> set[str]:
    """Every document token live in a ``Page.getFrameTree``, root included.

    The staleness guard reads this rather than the main frame's token alone.
    A UID minted inside an iframe carries that iframe's document, so checking
    it against the main frame's token refuses a node that is on the screen:
    measured, `click` on a button in a same-process iframe failed with
    "minted against a previous document" while the button was right there.

    Membership is the whole test. A token that names no live document is a
    stale UID, and that is still refused.
    """
    tokens: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        frame = node.get("frame", {})
        if frame:
            tokens.add(doc_token_from_frame(frame))
        for child in node.get("childFrames", []) or []:
            walk(child)

    walk(frame_tree_node)
    return tokens


async def read_live_doc_tokens(send: CdpSend) -> set[str]:
    """The tokens of every document in the page, from one CDP call."""
    result = await send(PAGE_GET_FRAME_TREE)
    return live_doc_tokens(result.get("frameTree", {}))


def read_live_doc_tokens_sync(send: Callable[..., dict[str, Any]]) -> set[str]:
    """Synchronous :func:`read_live_doc_tokens`."""
    return live_doc_tokens(send(PAGE_GET_FRAME_TREE).get("frameTree", {}))


def read_doc_token_sync(send: Callable[..., dict[str, Any]]) -> str:
    """Synchronous :func:`read_doc_token`, for the sync interaction driver."""
    result = send(PAGE_GET_FRAME_TREE)
    return doc_token_from_frame(result.get("frameTree", {}).get("frame", {}))


@dataclass(frozen=True)
class AxUidNode:
    """One accessibility node with its snapshot-stable UID.

    Attributes:
        uid: Stable identifier of the form ``"<docToken>-<backendNodeId>"``, valid
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


#: Reserves a UID while the node's subtree is walked, so a descendant cannot be
#: handed the same name. Always replaced by the real node before ``build``
#: returns.
_PLACEHOLDER = AxUidNode(
    uid="",
    role="",
    name="",
    value=None,
    backend_node_id=None,
    ax_node_id="",
    parent_uid=None,
    child_uids=(),
    ignored=True,
    properties={},
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
    doc_token: str
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

    def build(self, ax_result: dict[str, Any], *, doc_token: str) -> NativeSnapshot:
        """Build a snapshot from a raw ``Accessibility.getFullAXTree`` result.

        Assigns each node a UID naming its backend DOM node within
        ``doc_token``'s document, and installs the result as the current
        snapshot. Pure with respect to CDP transport: callable with a recorded
        response dict in a unit test.

        Building a second snapshot of the same document mints the same UIDs, so
        nothing a caller already holds is invalidated. The generation counter is
        kept as a diagnostic only; it is not part of a UID.

        Args:
            ax_result: The ``getFullAXTree`` result, ``{"nodes": [...]}``.
            doc_token: The document token every UID is stamped with, from
                :func:`read_doc_token`.

        Returns:
            The freshly built, current :class:`NativeSnapshot`.
        """
        self._generation += 1
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

        def mint(backend: int | None, node_token: str) -> str:
            """The node's UID: its backend DOM node, or an ordinal fallback.

            A node with no backend DOM node, and a node whose backend id another
            node already claimed, take ``x<ordinal>``. Both are unaddressable by
            an interaction, which is correct: the first is not in the DOM, and
            for the second the node earlier in document order owns the name.
            """
            nonlocal ordinal
            ordinal += 1
            if backend is not None:
                candidate = make_uid(node_token, backend)
                if candidate not in by_uid:
                    return candidate
            return f"{node_token}-{NON_DOM_UID_PREFIX}{ordinal}"

        def walk(ax_id: str, parent_uid: str | None) -> str | None:
            raw = by_ax_id.get(ax_id)
            if raw is None:
                return None
            raw_backend = raw.get("backendDOMNodeId")
            backend = int(raw_backend) if isinstance(raw_backend, int) else None
            # The node's own document, not the build's. A stitched tree carries
            # nodes from several documents, and a backendDOMNodeId is unique
            # within a renderer process rather than within a browser: measured,
            # a cross-origin child's ids overlapped the page's on 5 of its 7
            # nodes. Minting both under one token gave the second node an
            # `x<ordinal>` UID, which no interaction can address.
            uid = mint(backend, str(raw.get(NODE_DOC_TOKEN_KEY) or doc_token))
            # Claim the UID before recursing: a descendant must not be handed
            # the same name, and the node object is only built after its
            # children (it carries their UIDs).
            by_uid[uid] = _PLACEHOLDER
            order.append(uid)
            child_uids: list[str] = []
            for child_ax_id in raw.get("childIds", []) or []:
                child_uid = walk(str(child_ax_id), uid)
                if child_uid is not None:
                    child_uids.append(child_uid)
            by_uid[uid] = AxUidNode(
                uid=uid,
                role=_ax_string(raw.get("role")) or "",
                name=_ax_string(raw.get("name")) or "",
                value=_ax_string(raw.get("value")),
                backend_node_id=backend,
                ax_node_id=str(ax_id),
                parent_uid=parent_uid,
                child_uids=tuple(child_uids),
                ignored=bool(raw.get("ignored", False)),
                properties=_carried_properties(raw),
            )
            return uid

        root_uid = walk(root_ax_id, None) if root_ax_id is not None else None

        snapshot = NativeSnapshot(
            generation=self._generation,
            doc_token=doc_token,
            root_uid=root_uid,
            nodes=tuple(by_uid[uid] for uid in order),
            _by_uid=by_uid,
        )
        self._current = snapshot
        return snapshot

    async def snapshot(self, send: CdpSend) -> NativeSnapshot:
        """Enable the Accessibility domain, read the full tree, build a snapshot.

        Reads the live document token first, so every UID it mints is stamped
        with the document the tree was read from.

        Args:
            send: Async CDP transport, ``send(method, params) -> result``
                (a bound :meth:`~browser_tools.cdp_client.CDPClient.send`).

        Returns:
            The freshly built, current :class:`NativeSnapshot`.
        """
        doc_token = await read_doc_token(send)
        await send(AX_ENABLE)
        result = await send(AX_GET_FULL_TREE)
        return self.build(result, doc_token=doc_token)

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
        merged, doc_token = await read_stitched_ax_tree(send)
        return self.build(merged, doc_token=doc_token)

    def resolve_uid(self, uid: str) -> AxUidNode | None:
        """Resolve a UID against the current snapshot.

        This is a *tree* lookup: it answers what a node's role, name and
        children are, and it needs a snapshot in hand to answer. It is not the
        staleness guard, and the interaction path does not go through it -- a
        UID carries its own document token and backend node, so ``click`` and
        ``fill`` resolve without any tree at all.
        """
        if self._current is None:
            return None
        return self._current.resolve(uid)

    def backend_node_for_uid(self, uid: str) -> int | None:
        """Backend DOM node a UID resolves to in the current snapshot."""
        node = self.resolve_uid(uid)
        return node.backend_node_id if node is not None else None

    def note_navigation(self) -> None:
        """Drop the cached snapshot after a navigation.

        Invalidation is no longer this method's job, and does not depend on it
        being called. A navigation changes the document's ``loaderId``, so every
        outstanding UID carries a token that no longer matches and is refused on
        its own evidence -- including in a fresh process that never saw the
        navigation happen. This only releases a tree that now describes a
        document that is gone.
        """
        self._current = None
        self._generation += 1


@dataclass(frozen=True)
class ChildFrameTree:
    """One child frame's accessibility tree, and where it hangs in the parent.

    ``owner_doc_token`` is the *parent's* document, not this frame's. The
    owner node is an ``Iframe`` in the parent document, and it is found by
    backend node id, which is only unique within one renderer. Without the
    parent's token, a nested frame's owner lookup can land on a node of the
    same backend id in an unrelated document.
    """

    #: `DOM.getFrameOwner`'s backendNodeId: the parent's `Iframe` node.
    owner_backend_node_id: int
    #: The document that `Iframe` node lives in.
    owner_doc_token: str
    #: This frame's own document, stamped on every node it contributes.
    doc_token: str
    #: This frame's `Accessibility.getFullAXTree` result.
    result: dict[str, Any]


def stitch_ax_frames(
    top_result: dict[str, Any],
    child_frames: list[ChildFrameTree],
    *,
    top_doc_token: str,
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
    merged: list[dict[str, Any]] = []
    #: Owner lookup, by the document the node is in and its backend id. Keyed
    #: by the pair because a backend node id is unique within one renderer
    #: process, not within a browser.
    by_owner: dict[tuple[str, int], dict[str, Any]] = {}

    def take(nodes: list[dict[str, Any]], token: str) -> None:
        for node in nodes:
            node[NODE_DOC_TOKEN_KEY] = token
            backend = node.get("backendDOMNodeId")
            if isinstance(backend, int):
                by_owner.setdefault((token, backend), node)
            merged.append(node)

    take([dict(node) for node in top_result.get("nodes", []) or []], top_doc_token)

    for index, child in enumerate(child_frames):
        prefix = f"f{index + 1}:"
        child_root_id: str | None = None
        child_nodes: list[dict[str, Any]] = []
        for raw in child.result.get("nodes", []) or []:
            node = dict(raw)
            node["nodeId"] = prefix + str(node.get("nodeId"))
            parent_id = node.get("parentId")
            if parent_id is not None:
                node["parentId"] = prefix + str(parent_id)
            else:
                child_root_id = node["nodeId"]
            node["childIds"] = [prefix + str(cid) for cid in (node.get("childIds") or [])]
            child_nodes.append(node)

        owner = by_owner.get((child.owner_doc_token, child.owner_backend_node_id))
        if owner is not None and child_root_id is not None:
            owner["childIds"] = [*(owner.get("childIds") or []), child_root_id]
            for node in child_nodes:
                if node["nodeId"] == child_root_id:
                    node["parentId"] = owner["nodeId"]
        # Taken whether or not the owner was found, so a frame whose owner is
        # missing still contributes its nodes rather than vanishing, and so a
        # frame nested below this one can still find its own owner here.
        take(child_nodes, child.doc_token)

    return {"nodes": merged}


def _iter_child_frames(
    frame_tree_node: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every child frame and its parent frame, in pre-order.

    Pre-order so a frame is always stitched after the frame that owns it:
    a nested frame's `Iframe` node lives in its parent's document, which has
    to be in the merged tree before the lookup for it can succeed.

    The parent comes back too because the owner node is found by backend node
    id within the parent's document, and that document has to be named.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def walk(node: dict[str, Any]) -> None:
        frame = node.get("frame", {})
        for child in node.get("childFrames", []) or []:
            child_frame = child.get("frame", {})
            if child_frame.get("id") is not None:
                pairs.append((child_frame, frame))
            walk(child)

    walk(frame_tree_node)
    return pairs


async def read_stitched_ax_tree(send: CdpSend) -> tuple[dict[str, Any], str]:
    """Read the full accessibility tree, stitched across child frames.

    Enables the Accessibility and Page domains, reads the top frame's tree,
    discovers each child frame and its owner DOM node, reads each child frame's
    tree, and returns the single merged result (see :func:`stitch_ax_frames`).
    A frame whose owner or tree cannot be read is skipped rather than failing the
    whole snapshot, so a broken child frame degrades to the top-frame tree.

    Args:
        send: Async CDP transport, ``send(method, params) -> result``.

    Returns:
        ``(merged, top_doc_token)``: a merged ``{"nodes": [...]}`` ready for
        :meth:`NativeSnapshotReader.build`, and the top frame's document
        token. The token comes back from here rather than from a second
        ``read_doc_token`` call, because this function reads the frame tree
        it is derived from anyway, and two reads could straddle a navigation
        and stamp the tree with the wrong document.
    """
    try:
        await send(PAGE_ENABLE)
        frame_tree = await send(PAGE_GET_FRAME_TREE)
    except Exception:
        frame_tree = {}
    top_frame = frame_tree.get("frameTree", {}).get("frame", {})
    top_token = doc_token_from_frame(top_frame)

    await send(AX_ENABLE)
    top = await send(AX_GET_FULL_TREE)

    pairs = _iter_child_frames(frame_tree.get("frameTree", {}))
    if not pairs:
        return stitch_ax_frames(top, [], top_doc_token=top_token), top_token

    child_frames: list[ChildFrameTree] = []
    for child_frame, parent_frame in pairs:
        frame_id = str(child_frame.get("id"))
        try:
            owner = await send(DOM_GET_FRAME_OWNER, {"frameId": frame_id})
            backend = owner.get("backendNodeId")
            if not isinstance(backend, int):
                continue
            child = await send(AX_GET_FULL_TREE, {"frameId": frame_id})
        except Exception:
            continue
        child_frames.append(
            ChildFrameTree(
                owner_backend_node_id=backend,
                owner_doc_token=doc_token_from_frame(parent_frame),
                doc_token=doc_token_from_frame(child_frame),
                result=child,
            )
        )

    return stitch_ax_frames(top, child_frames, top_doc_token=top_token), top_token


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
