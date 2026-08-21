"""Unit tests for the native accessibility snapshot read path (ticket #39).

These exercise the UID assignment and stability scheme against synthetic /
recorded ``Accessibility.getFullAXTree`` responses, with no live browser
(RFC-01 Testing Strategy; the corpus/live path is the parity harness). They
prove the two properties the RFC fixes:

- A UID resolves to the same node for the lifetime of its snapshot.
- A UID is reassigned (and the old one goes stale) after the next snapshot or a
  navigation.
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.native_snapshot import (
    AX_ENABLE,
    AX_GET_FULL_TREE,
    DOM_GET_FRAME_OWNER,
    PAGE_ENABLE,
    PAGE_GET_FRAME_TREE,
    AxUidNode,
    NativeSnapshot,
    NativeSnapshotReader,
    read_stitched_ax_tree,
    stitch_ax_frames,
)


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


def test_uid_format_is_generation_dash_ordinal():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    assert snap.root_uid == "1-1"
    assert all(node.uid.startswith("1-") for node in snap.nodes)


def test_uids_assigned_in_depth_first_document_order():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    # DFS from root: root, heading, form, textbox, button.
    order = [(node.uid, node.role) for node in snap.nodes]
    assert order == [
        ("1-1", "RootWebArea"),
        ("1-2", "heading"),
        ("1-3", "form"),
        ("1-4", "textbox"),
        ("1-5", "button"),
    ]


def test_child_uids_are_wired():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    root = snap.resolve("1-1")
    form = snap.resolve("1-3")
    assert root is not None and form is not None
    assert root.child_uids == ("1-2", "1-3")
    assert form.child_uids == ("1-4", "1-5")
    assert form.parent_uid == "1-1"


def test_role_name_value_and_backend_extracted():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    textbox = snap.resolve("1-4")
    assert textbox is not None
    assert (textbox.role, textbox.name, textbox.value) == ("textbox", "Email", "a@b.com")
    assert textbox.backend_node_id == 40
    button = snap.resolve("1-5")
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
    snap = NativeSnapshotReader().build(tree)
    checkbox = snap.resolve("1-2")
    assert checkbox is not None
    # Only the interactive-state subset is carried.
    assert checkbox.properties == {"checked": True}


# --------------------------------------------------------------------------- #
# UID stability within a snapshot
# --------------------------------------------------------------------------- #


def test_uid_resolves_to_same_node_within_snapshot():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    first = reader.backend_node_for_uid("1-5")
    second = reader.backend_node_for_uid("1-5")
    assert first == second == 50


def test_snapshot_is_immutable_and_resolution_is_repeatable():
    snap = NativeSnapshotReader().build(_form_tree())
    node_a = snap.resolve("1-4")
    node_b = snap.resolve("1-4")
    assert node_a is node_b
    assert isinstance(node_a, AxUidNode)


def test_ordinals_are_deterministic_across_two_reads_of_same_tree():
    reader = NativeSnapshotReader()
    first = reader.build(_form_tree())
    second = reader.build(_form_tree())
    # Same ordinal -> same node identity (role/name/backend); only the
    # generation prefix differs between the two snapshots.
    first_by_ordinal = {n.uid.split("-")[1]: (n.role, n.name, n.backend_node_id) for n in first.nodes}
    second_by_ordinal = {n.uid.split("-")[1]: (n.role, n.name, n.backend_node_id) for n in second.nodes}
    assert first_by_ordinal == second_by_ordinal
    assert first.generation == 1
    assert second.generation == 2


# --------------------------------------------------------------------------- #
# UID reassignment across snapshots and navigation
# --------------------------------------------------------------------------- #


def test_new_snapshot_supersedes_old_uids():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    assert reader.resolve_uid("1-5") is not None
    reader.build(_form_tree())
    # The old-generation UID is stale; the new-generation one resolves.
    assert reader.resolve_uid("1-5") is None
    assert reader.backend_node_for_uid("2-5") == 50


def test_generation_increments_per_snapshot():
    reader = NativeSnapshotReader()
    assert reader.build(_form_tree()).generation == 1
    assert reader.build(_form_tree()).generation == 2
    assert reader.build(_form_tree()).generation == 3


def test_navigation_invalidates_current_snapshot():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    assert reader.resolve_uid("1-5") is not None
    reader.note_navigation()
    assert reader.current is None
    assert reader.resolve_uid("1-5") is None
    assert reader.backend_node_for_uid("1-5") is None


def test_navigation_bumps_generation_so_post_nav_uids_differ():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())  # generation 1
    reader.note_navigation()  # generation -> 2
    post_nav = reader.build(_form_tree())  # generation 3
    assert post_nav.generation == 3
    assert post_nav.root_uid == "3-1"
    # A pre-navigation UID never collides with a post-navigation one.
    assert reader.resolve_uid("1-1") is None


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
    snap = NativeSnapshotReader().build(tree)
    # The ignored node still has a UID (the walk is total) ...
    assert snap.resolve("1-2") is not None
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
    snap = NativeSnapshotReader().build(tree)
    assert snap.root_uid == "1-1"
    root = snap.resolve("1-1")
    assert root is not None and root.role == "RootWebArea"


def test_empty_tree_yields_no_root_and_no_nodes():
    snap = NativeSnapshotReader().build({"nodes": []})
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
    snap = NativeSnapshotReader().build(tree)
    root = snap.resolve("1-1")
    assert root is not None
    assert root.child_uids == ("1-2",)


# --------------------------------------------------------------------------- #
# format_tree rendering
# --------------------------------------------------------------------------- #


def test_format_tree_renders_uid_tagged_indented_visible_nodes():
    snap = NativeSnapshotReader().build(_form_tree())
    text = snap.format_tree()
    lines = text.splitlines()
    assert lines[0] == '[uid=1-1] RootWebArea "Sign in"'
    assert '  [uid=1-2] heading "Welcome"' in lines
    assert "  [uid=1-3] form" in lines
    assert "    [uid=1-4] textbox \"Email\" = 'a@b.com'" in lines
    assert '    [uid=1-5] button "Submit"' in lines


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
        return {}


@pytest.mark.asyncio
async def test_async_snapshot_enables_domain_then_reads_full_tree():
    send = _FakeSend(_form_tree())
    reader = NativeSnapshotReader()
    snap = await reader.snapshot(send)
    assert send.methods == [AX_ENABLE, AX_GET_FULL_TREE]
    assert snap.root_uid == "1-1"
    assert reader.backend_node_for_uid("1-5") == 50


def test_native_snapshot_type_is_frozen():
    snap = NativeSnapshotReader().build(_form_tree())
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
    snap = NativeSnapshotReader().build(merged)
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
    snap = NativeSnapshotReader().build(merged)
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
