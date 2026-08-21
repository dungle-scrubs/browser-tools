"""Unit tests for the ARIA-snapshot parser and the capture adapter.

The parser runs against representative Playwright ARIA-snapshot YAML with no
browser. The capture adapter runs against a fake ``call_tool`` session, so the
capture pipeline (navigate -> settle -> snapshot -> uid targets -> text) is
exercised end to end without launching Chrome. Only ``run_baseline`` /
``@pytest.mark.parity`` needs a real browser.
"""

from __future__ import annotations

from typing import Any

from parity_comparison import SnapshotNode, compare_captures
from parity_corpus import corpus_page
from parity_engines import (
    AriaSnapshotEngine,
    NativeSnapshotEngine,
    ParityEngine,
    capture_corpus,
    parse_aria_snapshot,
)

SAMPLE = """
- heading "Parity Form Page" [level=1]
- paragraph: Static prose here
- navigation "Primary":
  - link "First link":
    - /url: https://example.com/one
  - link "Second link"
- textbox "Email": current@value.com
- checkbox "Subscribe"
- button "Submit"
""".strip()


def test_parse_extracts_role_name_value():
    nodes = parse_aria_snapshot(SAMPLE)
    keys = [n.key() for n in nodes]
    assert ("heading", "Parity Form Page", None) in keys
    assert ("paragraph", "", "Static prose here") in keys
    assert ("textbox", "Email", "current@value.com") in keys
    assert ("button", "Submit", None) in keys


def test_parse_keeps_both_links():
    nodes = parse_aria_snapshot(SAMPLE)
    links = [n for n in nodes if n.role == "link"]
    assert {n.name for n in links} == {"First link", "Second link"}


def test_parse_skips_property_lines():
    nodes = parse_aria_snapshot(SAMPLE)
    assert all(n.role != "/url" for n in nodes)
    assert not any(n.role.startswith("/") for n in nodes)


def test_parse_handles_trailing_colon_container():
    nodes = parse_aria_snapshot('- navigation "Primary":')
    assert nodes == [SnapshotNode(role="navigation", name="Primary", value=None)]


def test_parse_handles_role_only_line():
    nodes = parse_aria_snapshot("- list")
    assert nodes == [SnapshotNode(role="list", name="", value=None)]


def test_parse_ignores_blank_and_garbage_lines():
    nodes = parse_aria_snapshot("\n   \nnot a node line\n- button \"Go\"\n")
    assert nodes == [SnapshotNode(role="button", name="Go", value=None)]


# --------------------------------------------------------------------------- #
# Capture adapter against a fake session
# --------------------------------------------------------------------------- #


class FakeSession:
    """A ``call_tool`` session that returns canned responses, no browser."""

    def __init__(self, tree: str, uid_targets: dict[str, str], text: str) -> None:
        self._tree = tree
        self._uid_targets = uid_targets
        self._text = text
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((tool, args or {}))
        if tool == "navigate":
            return {"result": {"title": "T", "url": args["url"], "interstitial": {"detected": False}}}
        if tool == "snapshot":
            return {"result": {"tree": self._tree}}
        if tool == "evaluate":
            script = (args or {})["script"]
            if "document.body.innerText" in script:
                return {"result": {"value": self._text}}
            if "querySelectorAll" in script:
                return {"result": {"value": self._uid_targets}}
            # settle script
            return {"result": {"value": True}}
        return {"error": f"unexpected tool {tool}"}


def test_capture_builds_a_page_capture():
    session = FakeSession(
        tree=SAMPLE,
        uid_targets={"#email": "html>body>form#signup>input#email"},
        text="Parity Form Page\nStatic prose here",
    )
    engine = AriaSnapshotEngine(session)
    capture = engine.capture(corpus_page("form"))

    assert capture.page_id == "form"
    assert ("button", "Submit", None) in [n.key() for n in capture.nodes]
    assert capture.uid_targets == {"#email": "html>body>form#signup>input#email"}
    assert capture.text == "Parity Form Page\nStatic prose here"


def test_capture_navigates_settles_then_snapshots_in_order():
    session = FakeSession(tree="- button \"Go\"", uid_targets={}, text="Go")
    AriaSnapshotEngine(session).capture(corpus_page("plain"))
    order = [tool for tool, _ in session.calls]
    assert order[0] == "navigate"
    assert "snapshot" in order
    # A settle evaluate must run before the snapshot so post-paint content is present.
    assert order.index("evaluate") < order.index("snapshot")


def test_two_identical_capture_runs_are_self_parity():
    """A stable page captured twice must match itself - the flake-free guarantee."""
    make = lambda: AriaSnapshotEngine(
        FakeSession(tree=SAMPLE, uid_targets={"#email": "path-a"}, text="Parity Form Page")
    ).capture(corpus_page("form"))
    assert compare_captures(make(), make()).matched


def test_capture_corpus_runs_all_pages():
    class OnePageEngine:
        name = "fake"

        def capture(self, page: Any) -> Any:
            return AriaSnapshotEngine(
                FakeSession(tree="- heading \"H\"", uid_targets={}, text="H")
            ).capture(page)

    captures = capture_corpus(OnePageEngine())
    assert set(captures) == {"plain", "form", "iframe", "shadow", "dynamic"}


# --------------------------------------------------------------------------- #
# Native CDP snapshot engine against a fake CDP session (no browser)
# --------------------------------------------------------------------------- #

# A recorded Accessibility.getFullAXTree response for a small form page.
_NATIVE_AX_TREE = {
    "nodes": [
        {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Parity Form Page"},
         "childIds": ["2", "3"], "backendDOMNodeId": 1, "ignored": False},
        {"nodeId": "2", "parentId": "1", "role": {"value": "heading"},
         "name": {"value": "Parity Form Page"}, "childIds": [], "backendDOMNodeId": 2, "ignored": False},
        {"nodeId": "3", "parentId": "1", "role": {"value": "textbox"}, "name": {"value": "Email"},
         "value": {"value": "current@value.com"}, "childIds": [], "backendDOMNodeId": 3, "ignored": False},
        {"nodeId": "4", "parentId": "1", "role": {"value": "presentation"}, "name": {"value": ""},
         "childIds": [], "backendDOMNodeId": 4, "ignored": True},
    ]
}


class FakeNativeCdpSession:
    """A native-engine session that returns canned CDP responses, no browser."""

    def __init__(self, ax_tree: dict[str, Any], uid_targets: dict[str, str], text: str) -> None:
        self._ax_tree = ax_tree
        self._uid_targets = uid_targets
        self._text = text
        self.calls: list[str] = []

    def navigate(self, url: str) -> None:
        self.calls.append(f"navigate:{url}")

    def get_full_ax_tree(self) -> dict[str, Any]:
        self.calls.append("get_full_ax_tree")
        return self._ax_tree

    def get_stitched_ax_tree(self) -> dict[str, Any]:
        # The native engines read the cross-frame-stitched tree (#41); the fake's
        # canned single-frame tree needs no stitching, so it serves it directly.
        self.calls.append("get_stitched_ax_tree")
        return self._ax_tree

    def evaluate(self, script: str) -> Any:
        self.calls.append("evaluate")
        if "document.body.innerText" in script:
            return self._text
        if "querySelectorAll" in script:
            return self._uid_targets
        return True  # settle script


def test_native_engine_satisfies_the_parity_engine_protocol():
    engine = NativeSnapshotEngine(FakeNativeCdpSession(_NATIVE_AX_TREE, {}, ""))
    assert isinstance(engine, ParityEngine)
    assert engine.name == "native-cdp"


def test_native_capture_builds_node_set_from_ax_tree():
    session = FakeNativeCdpSession(
        _NATIVE_AX_TREE,
        uid_targets={"#email": "html>body>form#signup>input#email"},
        text="Parity Form Page",
    )
    capture = NativeSnapshotEngine(session).capture(corpus_page("form"))

    assert capture.page_id == "form"
    keys = [n.key() for n in capture.nodes]
    assert ("heading", "Parity Form Page", None) in keys
    assert ("textbox", "Email", "current@value.com") in keys
    # The ignored node is excluded from the node set.
    assert all(n.role != "presentation" for n in capture.nodes)
    assert capture.uid_targets == {"#email": "html>body>form#signup>input#email"}
    assert capture.text == "Parity Form Page"


def test_native_capture_carries_native_uid_and_backend_node():
    session = FakeNativeCdpSession(_NATIVE_AX_TREE, {}, "")
    capture = NativeSnapshotEngine(session).capture(corpus_page("form"))
    textbox = next(n for n in capture.nodes if n.role == "textbox")
    # Native snapshot UIDs are "<generation>-<ordinal>"; the textbox is the
    # third node in document order of the first snapshot after navigation.
    assert textbox.uid == "2-3"
    assert textbox.backend_node == "3"


def test_native_capture_navigates_settles_then_reads_tree_in_order():
    session = FakeNativeCdpSession(_NATIVE_AX_TREE, {}, "")
    NativeSnapshotEngine(session).capture(corpus_page("plain"))
    assert session.calls[0].startswith("navigate:")
    assert "get_stitched_ax_tree" in session.calls
    # A settle evaluate must run before the AX tree read.
    assert session.calls.index("evaluate") < session.calls.index("get_stitched_ax_tree")


def test_native_engine_is_self_parity_on_a_stable_page():
    """The native candidate captured twice matches itself (flake-free shape)."""
    make = lambda: NativeSnapshotEngine(
        FakeNativeCdpSession(_NATIVE_AX_TREE, {"#email": "path-a"}, "Parity Form Page")
    ).capture(corpus_page("form"))
    assert compare_captures(make(), make()).matched
