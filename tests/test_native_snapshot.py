"""Unit tests for the native accessibility snapshot read path (ticket #39).

These exercise the UID assignment and stability scheme against synthetic /
recorded ``Accessibility.getFullAXTree`` responses, with no live browser
(RFC-01 Testing Strategy; the corpus/live path is the parity harness). They
prove the two properties the RFC fixes:

- A UID names the same DOM node for the lifetime of the *document*, not of the
  snapshot. Taking another snapshot mints the same UIDs and invalidates
  nothing.
- A UID carries the document it was minted against, so one from a previous
  document is recognizable as such without any snapshot in hand.

The scheme this replaced stamped UIDs with a per-reader snapshot counter and a
depth-first ordinal. Every CLI invocation starts that counter at 1, so a UID
from one process resolved against another process's tree by ordinal and named
whatever node now sat at that position (#96).
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.native_snapshot import (
    AX_ENABLE,
    AX_GET_FULL_TREE,
    DOM_GET_FRAME_OWNER,
    NON_DOM_UID_PREFIX,
    PAGE_ENABLE,
    PAGE_GET_FRAME_TREE,
    AxUidNode,
    NativeSnapshot,
    NativeSnapshotReader,
    doc_token_from_loader_id,
    parse_uid,
    read_stitched_ax_tree,
    stitch_ax_frames,
)

# A loaderId as Chrome reports it, and the token derived from it.
LOADER_ID = "D0C0FFEE1234ABCDEF0123456789ABCD"
DOC = doc_token_from_loader_id(LOADER_ID)
OTHER_DOC = doc_token_from_loader_id("0B50LETE5678FEDCBA9876543210FEDC")


def _node(
    node_id: str,
    role: str,
    name: str = "",
    *,
    parent: str | None = None,
    children: list[str] | None = None,
    backend: int | None = None,
    value: str | None = None,
    ignored: bool = False,
    properties: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one synthetic CDP AX node in ``getFullAXTree`` shape."""
    raw: dict[str, Any] = {
        "nodeId": node_id,
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "childIds": children or [],
        "ignored": ignored,
    }
    if parent is not None:
        raw["parentId"] = parent
    if backend is not None:
        raw["backendDOMNodeId"] = backend
    if value is not None:
        raw["value"] = {"type": "computedString", "value": value}
    if properties is not None:
        raw["properties"] = properties
    return raw


def _form_tree() -> dict[str, Any]:
    """A small form: root > (heading, form > (textbox, button))."""
    return {
        "nodes": [
            _node("1", "RootWebArea", "Sign in", children=["2", "3"], backend=10),
            _node("2", "heading", "Welcome", parent="1", backend=20),
            _node("3", "form", "", parent="1", children=["4", "5"], backend=30),
            _node("4", "textbox", "Email", parent="3", backend=40, value="a@b.com"),
            _node("5", "button", "Submit", parent="3", backend=50),
        ]
    }


# --------------------------------------------------------------------------- #
# UID assignment
# --------------------------------------------------------------------------- #


def test_uid_format_is_doc_token_dash_backend_node():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree(), doc_token=DOC)
    assert snap.root_uid == f"{DOC}-10"
    assert all(node.uid.startswith(f"{DOC}-") for node in snap.nodes)
    # The tail is the node's own backend DOM node, not its position.
    assert all(
        node.uid == f"{DOC}-{node.backend_node_id}"
        for node in snap.nodes
        if node.backend_node_id is not None
    )


def test_a_node_with_no_backend_node_gets_an_unaddressable_uid():
    """AX-internal leaves still need a name; it must not look like a DOM node."""
    tree = {
        "nodes": [
            _node("1", "RootWebArea", children=["2"], backend=10),
            _node("2", "InlineTextBox", "hello", parent="1"),
        ]
    }
    snap = NativeSnapshotReader().build(tree, doc_token=DOC)
    leaf = snap.nodes[1]
    assert leaf.backend_node_id is None
    assert leaf.uid.startswith(f"{DOC}-x")
    assert not leaf.uid.rsplit("-", 1)[1].isdigit()


def test_a_repeated_backend_node_keeps_the_first_in_document_order():
    """Two AX nodes on one DOM node: the earlier one owns the addressable uid."""
    tree = {
        "nodes": [
            _node("1", "RootWebArea", children=["2", "3"], backend=10),
            _node("2", "button", "Go", parent="1", backend=20),
            _node("3", "generic", "Go", parent="1", backend=20),
        ]
    }
    snap = NativeSnapshotReader().build(tree, doc_token=DOC)
    assert snap.nodes[1].uid == f"{DOC}-20"
    assert snap.nodes[2].uid.startswith(f"{DOC}-x")
    assert len({n.uid for n in snap.nodes}) == 3, "uids must stay unique"


def test_nodes_are_returned_in_depth_first_document_order():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree(), doc_token=DOC)
    # DFS from root: root, heading, form, textbox, button.
    order = [(node.uid, node.role) for node in snap.nodes]
    assert order == [
        (f"{DOC}-10", "RootWebArea"),
        (f"{DOC}-20", "heading"),
        (f"{DOC}-30", "form"),
        (f"{DOC}-40", "textbox"),
        (f"{DOC}-50", "button"),
    ]


def test_child_uids_are_wired():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree(), doc_token=DOC)
    root = snap.resolve(f"{DOC}-10")
    form = snap.resolve(f"{DOC}-30")
    assert root is not None and form is not None
    assert root.child_uids == (f"{DOC}-20", f"{DOC}-30")
    assert form.child_uids == (f"{DOC}-40", f"{DOC}-50")
    assert form.parent_uid == f"{DOC}-10"


def test_role_name_value_and_backend_extracted():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree(), doc_token=DOC)
    textbox = snap.resolve(f"{DOC}-40")
    assert textbox is not None
    assert (textbox.role, textbox.name, textbox.value) == ("textbox", "Email", "a@b.com")
    assert textbox.backend_node_id == 40
    button = snap.resolve(f"{DOC}-50")
    assert button is not None and button.value is None


def test_carried_properties_extracted():
    tree = {
        "nodes": [
            _node("1", "RootWebArea", children=["2"], backend=1),
            _node(
                "2",
                "checkbox",
                "Subscribe",
                parent="1",
                backend=2,
                properties=[
                    {"name": "checked", "value": {"value": True}},
                    {"name": "focusable", "value": {"value": True}},
                ],
            ),
        ]
    }
    snap = NativeSnapshotReader().build(tree, doc_token=DOC)
    checkbox = snap.resolve(f"{DOC}-2")
    assert checkbox is not None
    # Only the interactive-state subset is carried.
    assert checkbox.properties == {"checked": True}


# --------------------------------------------------------------------------- #
# UID stability within a snapshot
# --------------------------------------------------------------------------- #


def test_uid_resolves_to_same_node_within_snapshot():
    reader = NativeSnapshotReader()
    reader.build(_form_tree(), doc_token=DOC)
    first = reader.backend_node_for_uid(f"{DOC}-50")
    second = reader.backend_node_for_uid(f"{DOC}-50")
    assert first == second == 50


def test_snapshot_is_immutable_and_resolution_is_repeatable():
    snap = NativeSnapshotReader().build(_form_tree(), doc_token=DOC)
    node_a = snap.resolve(f"{DOC}-40")
    node_b = snap.resolve(f"{DOC}-40")
    assert node_a is node_b
    assert isinstance(node_a, AxUidNode)


def test_two_reads_of_one_tree_bind_each_uid_to_the_same_node():
    reader = NativeSnapshotReader()
    first = reader.build(_form_tree(), doc_token=DOC)
    second = reader.build(_form_tree(), doc_token=DOC)
    identity = lambda snap: {
        n.uid: (n.role, n.name, n.backend_node_id) for n in snap.nodes
    }
    assert identity(first) == identity(second)


# --------------------------------------------------------------------------- #
# UID lifetime: the document, not the snapshot
#
# These four were written against the opposite rule -- a new snapshot or a
# navigation superseded every outstanding UID. That rule is what made the silent
# wrong-field failure possible, so each is rewritten to the rule that replaced
# it rather than deleted (#96).
# --------------------------------------------------------------------------- #


def test_a_new_snapshot_preserves_every_earlier_uid():
    """Was ``test_new_snapshot_supersedes_old_uids``, inverted with the rule."""
    reader = NativeSnapshotReader()
    first = reader.build(_form_tree(), doc_token=DOC)
    before = {node.uid for node in first.nodes}

    second = reader.build(_form_tree(), doc_token=DOC)

    assert {node.uid for node in second.nodes} == before, (
        "a second snapshot of the same document must mint the same uids"
    )
    for uid in before:
        assert reader.resolve_uid(uid) is not None
    assert reader.backend_node_for_uid(f"{DOC}-50") == 50


def test_only_the_unaddressable_names_can_move_when_the_tree_changes():
    """A DOM-backed uid survives a tree change; an ordinal fallback need not.

    The ``x<ordinal>`` form has no DOM node to key on, so it is positional and
    can shift when nodes appear or disappear. That is harmless by construction:
    the interaction path refuses it, so no shift can misdirect a click or fill.
    Every uid that *can* be acted on is keyed to its backend DOM node and holds.
    """
    reader = NativeSnapshotReader()
    before = reader.build(_form_tree(), doc_token=DOC)

    grown = _form_tree()
    grown["nodes"][0]["childIds"] = ["2", "3", "6"]
    grown["nodes"].append(_node("6", "status", "Saved", parent="1", backend=60))
    grown["nodes"].append(_node("7", "InlineTextBox", "Saved", parent="6"))
    grown["nodes"][5]["childIds"] = ["7"]
    after = reader.build(grown, doc_token=DOC)

    addressable = lambda snap: {
        n.uid for n in snap.nodes if n.backend_node_id is not None
    }
    assert addressable(before) <= addressable(after), (
        "a uid naming a DOM node that still exists must survive a tree change"
    )
    moved = {n.uid for n in before.nodes} - {n.uid for n in after.nodes}
    assert all(f"-{NON_DOM_UID_PREFIX}" in uid for uid in moved), (
        f"an addressable uid moved: {sorted(moved)}"
    )


def test_the_generation_counter_is_no_longer_part_of_a_uid():
    """Was ``test_generation_increments_per_snapshot``.

    It still counts snapshots, as a diagnostic. It just names nothing.
    """
    reader = NativeSnapshotReader()
    first = reader.build(_form_tree(), doc_token=DOC)
    second = reader.build(_form_tree(), doc_token=DOC)

    assert (first.generation, second.generation) == (1, 2)
    assert first.root_uid == second.root_uid == f"{DOC}-10"


def test_a_uid_from_another_document_is_recognizable_without_a_snapshot():
    """Was ``test_navigation_invalidates_current_snapshot``.

    Invalidation no longer depends on the reader being told a navigation
    happened. The UID carries its document, so a fresh process that never saw
    the navigation reaches the same verdict.
    """
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree(), doc_token=DOC)
    old_uid = f"{DOC}-50"
    assert old_uid in {node.uid for node in snap.nodes}

    after_nav = reader.build(_form_tree(), doc_token=OTHER_DOC)

    assert old_uid not in {node.uid for node in after_nav.nodes}
    assert after_nav.doc_token == OTHER_DOC
    # The evidence is in the uid itself, not in reader state.
    assert parse_uid(old_uid)[0] != after_nav.doc_token


def test_uids_from_two_documents_never_collide():
    """Was ``test_navigation_bumps_generation_so_post_nav_uids_differ``.

    Same intent -- a pre-navigation UID must never name a post-navigation node
    -- expressed as the docToken change that now carries it.
    """
    reader = NativeSnapshotReader()
    before = reader.build(_form_tree(), doc_token=DOC)
    reader.note_navigation()
    after = reader.build(_form_tree(), doc_token=OTHER_DOC)

    assert not ({n.uid for n in before.nodes} & {n.uid for n in after.nodes})
    # Identical backend nodes, so under the old scheme these were identical uids.
    assert [n.backend_node_id for n in before.nodes] == [
        n.backend_node_id for n in after.nodes
    ]


# --------------------------------------------------------------------------- #
# Ignored nodes and node-set filtering
# --------------------------------------------------------------------------- #


def test_ignored_nodes_get_a_uid_but_are_excluded_from_visible_set():
    tree = {
        "nodes": [
            _node("1", "RootWebArea", children=["2", "3"], backend=1),
            _node("2", "presentation", parent="1", backend=2, ignored=True),
            _node("3", "button", "Go", parent="1", backend=3),
        ]
    }
    snap = NativeSnapshotReader().build(tree, doc_token=DOC)
    # The ignored node still has a UID (the walk is total) ...
    assert snap.resolve(f"{DOC}-2") is not None
    # ... but it is filtered from the visible node set.
    visible_roles = [n.role for n in snap.visible_nodes()]
    assert "presentation" not in visible_roles
    assert visible_roles == ["RootWebArea", "button"]


# --------------------------------------------------------------------------- #
# Root detection and edge cases
# --------------------------------------------------------------------------- #


def test_root_is_node_whose_parent_is_outside_the_returned_set():
    # Root carries a parentId that is not present in the node list.
    tree = {
        "nodes": [
            _node("100", "RootWebArea", parent="99", children=["101"], backend=1),
            _node("101", "button", "Go", parent="100", backend=2),
        ]
    }
    snap = NativeSnapshotReader().build(tree, doc_token=DOC)
    assert snap.root_uid == f"{DOC}-1"
    root = snap.resolve(f"{DOC}-1")
    assert root is not None and root.role == "RootWebArea"


def test_empty_tree_yields_no_root_and_no_nodes():
    snap = NativeSnapshotReader().build({"nodes": []}, doc_token=DOC)
    assert snap.root_uid is None
    assert snap.nodes == ()
    assert snap.format_tree() == "(empty accessibility tree)"


def test_missing_child_ids_are_skipped_without_error():
    tree = {
        "nodes": [
            _node("1", "RootWebArea", children=["2", "missing"], backend=1),
            _node("2", "button", "Go", parent="1", backend=2),
        ]
    }
    snap = NativeSnapshotReader().build(tree, doc_token=DOC)
    root = snap.resolve(f"{DOC}-1")
    assert root is not None
    assert root.child_uids == (f"{DOC}-2",)


# --------------------------------------------------------------------------- #
# format_tree rendering
# --------------------------------------------------------------------------- #


def test_format_tree_renders_uid_tagged_indented_visible_nodes():
    snap = NativeSnapshotReader().build(_form_tree(), doc_token=DOC)
    text = snap.format_tree()
    lines = text.splitlines()
    assert lines[0] == f'[uid={DOC}-10] RootWebArea "Sign in"'
    assert f'  [uid={DOC}-20] heading "Welcome"' in lines
    assert f"  [uid={DOC}-30] form" in lines
    assert f"    [uid={DOC}-40] textbox \"Email\" = 'a@b.com'" in lines
    assert f'    [uid={DOC}-50] button "Submit"' in lines


# --------------------------------------------------------------------------- #
# Async entry point over a fake CDP transport
# --------------------------------------------------------------------------- #


class _FakeSend:
    """Records CDP methods and returns a canned getFullAXTree result."""

    def __init__(self, tree: dict[str, Any]) -> None:
        self._tree = tree
        self.methods: list[str] = []

    async def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.methods.append(method)
        if method == AX_GET_FULL_TREE:
            return self._tree
        if method == PAGE_GET_FRAME_TREE:
            return {"frameTree": {"frame": {"loaderId": LOADER_ID}}}
        return {}


@pytest.mark.asyncio
async def test_async_snapshot_reads_the_document_token_then_the_tree():
    """The token comes first: every uid the build mints is stamped with it."""
    send = _FakeSend(_form_tree())
    reader = NativeSnapshotReader()
    snap = await reader.snapshot(send)
    assert send.methods == [PAGE_GET_FRAME_TREE, AX_ENABLE, AX_GET_FULL_TREE]
    assert snap.doc_token == DOC
    assert snap.root_uid == f"{DOC}-10"
    assert reader.backend_node_for_uid(f"{DOC}-50") == 50


def test_native_snapshot_type_is_frozen():
    snap = NativeSnapshotReader().build(_form_tree(), doc_token=DOC)
    assert isinstance(snap, NativeSnapshot)
    with pytest.raises(AttributeError):
        snap.generation = 99  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Cross-frame stitching (ticket #41)
# --------------------------------------------------------------------------- #


def _top_with_iframe() -> dict[str, Any]:
    """Top-frame tree: a root, a heading, and an Iframe node owning backend 14."""
    return {
        "nodes": [
            _node("1", "RootWebArea", "Host", children=["2", "3"], backend=4),
            _node("2", "heading", "Host heading", parent="1", backend=12),
            _node("3", "Iframe", "Child frame", parent="1", backend=14),
        ]
    }


def _child_tree() -> dict[str, Any]:
    """Child-frame tree with its own colliding node ids (1, 2)."""
    return {
        "nodes": [
            _node("1", "RootWebArea", "Child", children=["2"], backend=5),
            _node("2", "button", "Framed button", parent="1", backend=23),
        ]
    }


def test_stitch_splices_child_under_its_owner_iframe_node():
    merged = stitch_ax_frames(_top_with_iframe(), [(14, _child_tree())])
    snap = NativeSnapshotReader().build(merged, doc_token=DOC)
    roles = [(n.role, n.name) for n in snap.visible_nodes()]
    # The child frame's nodes now appear, spliced under the Iframe node.
    assert ("Iframe", "Child frame") in roles
    assert ("RootWebArea", "Child") in roles
    assert ("button", "Framed button") in roles
    # The Iframe node's child is the child frame's (namespaced) root.
    iframe = next(n for n in snap.nodes if n.role == "Iframe")
    child_root = next(n for n in snap.nodes if n.role == "RootWebArea" and n.name == "Child")
    assert child_root.uid in iframe.child_uids
    assert child_root.parent_uid == iframe.uid


def test_stitch_namespaces_colliding_child_ids():
    """The child's ax_node_id 1/2 collide with the top's; both survive stitching."""
    merged = stitch_ax_frames(_top_with_iframe(), [(14, _child_tree())])
    ids = [n["nodeId"] for n in merged["nodes"]]
    assert ids.count("1") == 1  # only the top root keeps bare id "1"
    assert "f1:1" in ids and "f1:2" in ids


def test_stitch_with_no_children_is_identity():
    top = _top_with_iframe()
    assert stitch_ax_frames(top, []) == {"nodes": top["nodes"]}


def test_stitch_skips_frame_with_no_matching_owner():
    """An owner backend absent from the top tree drops the frame's link, not its nodes."""
    merged = stitch_ax_frames(_top_with_iframe(), [(999, _child_tree())])
    iframe = next(n for n in merged["nodes"] if n.get("role", {}).get("value") == "Iframe")
    # The Iframe node gains no child link when its owner backend is unknown.
    assert "f1:1" not in (iframe.get("childIds") or [])


class _FrameSend:
    """Async CDP fake for the stitched read: serves top + one child frame."""

    def __init__(self, top: dict[str, Any], child: dict[str, Any]) -> None:
        self._top = top
        self._child = child
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self.calls.append((method, params))
        if method == AX_GET_FULL_TREE:
            return self._child if params.get("frameId") else self._top
        if method == PAGE_GET_FRAME_TREE:
            return {
                "frameTree": {
                    "frame": {"id": "TOP"},
                    "childFrames": [{"frame": {"id": "CHILD"}}],
                }
            }
        if method == DOM_GET_FRAME_OWNER:
            return {"backendNodeId": 14}
        return {}


@pytest.mark.asyncio
async def test_read_stitched_ax_tree_discovers_and_splices_child_frame():
    send = _FrameSend(_top_with_iframe(), _child_tree())
    merged = await read_stitched_ax_tree(send)
    methods = [m for m, _ in send.calls]
    assert AX_ENABLE in methods and PAGE_ENABLE in methods and PAGE_GET_FRAME_TREE in methods
    # The child frame's tree was read with its frameId.
    assert (AX_GET_FULL_TREE, {"frameId": "CHILD"}) in send.calls
    snap = NativeSnapshotReader().build(merged, doc_token=DOC)
    assert ("button", "Framed button") in {(n.role, n.name) for n in snap.visible_nodes()}


@pytest.mark.asyncio
async def test_read_stitched_ax_tree_degrades_to_top_frame_when_no_children():
    class _NoFrames:
        async def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == AX_GET_FULL_TREE:
                return _form_tree()
            if method == PAGE_GET_FRAME_TREE:
                return {"frameTree": {"frame": {"id": "TOP"}}}
            return {}

    merged = await read_stitched_ax_tree(_NoFrames())
    # With no child frames the merged tree is exactly the top tree.
    assert merged == _form_tree()


@pytest.mark.asyncio
async def test_snapshot_stitched_builds_from_merged_tree():
    send = _FrameSend(_top_with_iframe(), _child_tree())
    reader = NativeSnapshotReader()
    snap = await reader.snapshot_stitched(send)
    assert reader.current is snap
    assert ("button", "Framed button") in {(n.role, n.name) for n in snap.visible_nodes()}
