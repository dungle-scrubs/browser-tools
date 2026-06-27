"""Tests for frame tree management (M-2.2, M-2.3)."""

from __future__ import annotations

import pytest

from browser_tools.frame_manager import FrameManager

# Sample frame tree mimicking Chrome's Page.getFrameTree response
SAMPLE_FRAME_TREE = {
    "frame": {
        "id": "MAIN",
        "url": "https://admin.shopify.com/store/myshop/apps/myapp",
        "securityOrigin": "https://admin.shopify.com",
        "name": "",
    },
    "childFrames": [
        {
            "frame": {
                "id": "IFRAME1",
                "url": "https://myapp.example.com/embedded",
                "securityOrigin": "https://myapp.example.com",
                "name": "app-iframe",
                "parentId": "MAIN",
            },
            "childFrames": [
                {
                    "frame": {
                        "id": "NESTED1",
                        "url": "https://auth.example.com/oauth/authorize",
                        "securityOrigin": "https://auth.example.com",
                        "name": "oauth-frame",
                        "parentId": "IFRAME1",
                    },
                    "childFrames": [],
                }
            ],
        },
        {
            "frame": {
                "id": "IFRAME2",
                "url": "https://analytics.example.com/widget",
                "securityOrigin": "https://analytics.example.com",
                "name": "analytics",
                "parentId": "MAIN",
            },
            "childFrames": [],
        },
    ],
}


@pytest.fixture
def fm() -> FrameManager:
    """Create a FrameManager populated with the sample frame tree."""
    manager = FrameManager()
    manager.update_from_frame_tree(SAMPLE_FRAME_TREE)
    return manager


class TestFrameTreeParsing:
    """Tests for parsing frame trees."""

    def test_root_frame_id(self, fm: FrameManager) -> None:
        """Root frame ID should match the top-level frame."""
        assert fm.root_frame_id == "MAIN"

    def test_all_frames_tracked(self, fm: FrameManager) -> None:
        """All frames in the tree should be tracked."""
        flat = fm.get_flat_frames()
        assert len(flat) == 4
        ids = {f["frameId"] for f in flat}
        assert ids == {"MAIN", "IFRAME1", "IFRAME2", "NESTED1"}

    def test_frame_depths(self, fm: FrameManager) -> None:
        """Frames should have correct nesting depth."""
        flat = fm.get_flat_frames()
        depth_map = {f["frameId"]: f["depth"] for f in flat}
        assert depth_map["MAIN"] == 0
        assert depth_map["IFRAME1"] == 1
        assert depth_map["IFRAME2"] == 1
        assert depth_map["NESTED1"] == 2

    def test_frame_tree_structure(self, fm: FrameManager) -> None:
        """Frame tree should preserve parent-child relationships."""
        tree = fm.get_frame_tree()
        assert tree is not None
        assert tree["frameId"] == "MAIN"
        assert len(tree["children"]) == 2
        child_ids = {c["frameId"] for c in tree["children"]}
        assert child_ids == {"IFRAME1", "IFRAME2"}

    def test_nested_children(self, fm: FrameManager) -> None:
        """Nested iframes should be children of their parent."""
        tree = fm.get_frame_tree()
        iframe1 = next(c for c in tree["children"] if c["frameId"] == "IFRAME1")
        assert len(iframe1["children"]) == 1
        assert iframe1["children"][0]["frameId"] == "NESTED1"

    def test_frame_urls(self, fm: FrameManager) -> None:
        """Frame URLs should be correctly parsed."""
        flat = fm.get_flat_frames()
        url_map = {f["frameId"]: f["url"] for f in flat}
        assert url_map["MAIN"] == "https://admin.shopify.com/store/myshop/apps/myapp"
        assert url_map["IFRAME1"] == "https://myapp.example.com/embedded"

    def test_empty_frame_tree(self) -> None:
        """Empty manager should return None for frame tree."""
        fm = FrameManager()
        assert fm.get_frame_tree() is None
        assert fm.get_flat_frames() == []


class TestFrameSelection:
    """Tests for frame selection by URL pattern (D-002)."""

    def test_select_by_url_substring(self, fm: FrameManager) -> None:
        """URL substring match should select the correct frame."""
        frame = fm.select_frame_by_url("myapp.example.com")
        assert frame is not None
        assert frame.frame_id == "IFRAME1"
        assert fm.selected_frame_id == "IFRAME1"

    def test_select_nested_frame(self, fm: FrameManager) -> None:
        """Should be able to select deeply nested frames."""
        frame = fm.select_frame_by_url("auth.example.com")
        assert frame is not None
        assert frame.frame_id == "NESTED1"

    def test_select_case_insensitive(self, fm: FrameManager) -> None:
        """URL matching should be case-insensitive."""
        frame = fm.select_frame_by_url("ANALYTICS.EXAMPLE.COM")
        assert frame is not None
        assert frame.frame_id == "IFRAME2"

    def test_select_no_match_returns_none(self, fm: FrameManager) -> None:
        """No match should return None and clear selection."""
        frame = fm.select_frame_by_url("nonexistent.com")
        assert frame is None
        assert fm.selected_frame_id is None

    def test_reset_frame(self, fm: FrameManager) -> None:
        """reset_frame should clear selection."""
        fm.select_frame_by_url("myapp.example.com")
        assert fm.selected_frame_id is not None
        fm.reset_frame()
        assert fm.selected_frame_id is None

    def test_selected_frame_returns_info(self, fm: FrameManager) -> None:
        """get_selected_frame should return the FrameInfo."""
        fm.select_frame_by_url("myapp.example.com")
        selected = fm.get_selected_frame()
        assert selected is not None
        assert selected.url == "https://myapp.example.com/embedded"

    def test_no_selection_returns_none(self, fm: FrameManager) -> None:
        """get_selected_frame with no selection returns None."""
        assert fm.get_selected_frame() is None


class TestFrameEvents:
    """Tests for frame lifecycle event handling."""

    def test_frame_attached(self, fm: FrameManager) -> None:
        """frameAttached should add a new frame."""
        fm.handle_frame_attached(
            {
                "frameId": "NEW_FRAME",
                "parentFrameId": "MAIN",
            }
        )
        flat = fm.get_flat_frames()
        ids = {f["frameId"] for f in flat}
        assert "NEW_FRAME" in ids

    def test_frame_detached(self, fm: FrameManager) -> None:
        """frameDetached should remove a frame."""
        fm.handle_frame_detached({"frameId": "IFRAME2"})
        flat = fm.get_flat_frames()
        ids = {f["frameId"] for f in flat}
        assert "IFRAME2" not in ids

    def test_frame_detached_clears_selection(self, fm: FrameManager) -> None:
        """Detaching the selected frame should clear selection."""
        fm.select_frame_by_url("myapp.example.com")
        assert fm.selected_frame_id == "IFRAME1"
        fm.handle_frame_detached({"frameId": "IFRAME1"})
        assert fm.selected_frame_id is None

    def test_frame_navigated_updates_url(self, fm: FrameManager) -> None:
        """frameNavigated should update the frame's URL."""
        fm.handle_frame_navigated(
            {
                "frame": {
                    "id": "IFRAME1",
                    "url": "https://myapp.example.com/dashboard",
                    "securityOrigin": "https://myapp.example.com",
                    "name": "app-iframe",
                }
            }
        )
        flat = fm.get_flat_frames()
        iframe1 = next(f for f in flat if f["frameId"] == "IFRAME1")
        assert iframe1["url"] == "https://myapp.example.com/dashboard"

    def test_frame_navigated_re_resolves_selection(self, fm: FrameManager) -> None:
        """In-frame navigation should re-resolve URL pattern selection (D-002)."""
        fm.select_frame_by_url("myapp.example.com")
        assert fm.selected_frame_id == "IFRAME1"

        # Simulate in-frame OAuth redirect
        fm.handle_frame_navigated(
            {
                "frame": {
                    "id": "IFRAME1",
                    "url": "https://myapp.example.com/oauth/callback",
                    "securityOrigin": "https://myapp.example.com",
                    "name": "app-iframe",
                }
            }
        )
        # Selection should survive because URL still matches pattern
        assert fm.selected_frame_id == "IFRAME1"


class TestExecutionContexts:
    """Tests for execution context mapping."""

    def test_context_mapped_to_frame(self, fm: FrameManager) -> None:
        """executionContextCreated should map context to frame."""
        fm.handle_execution_context_created(
            {
                "context": {
                    "id": 42,
                    "auxData": {"frameId": "IFRAME1", "isDefault": True},
                }
            }
        )
        fm.select_frame_by_url("myapp.example.com")
        assert fm.get_selected_execution_context_id() == 42

    def test_context_destroyed_clears_mapping(self, fm: FrameManager) -> None:
        """executionContextDestroyed should clear the mapping."""
        fm.handle_execution_context_created(
            {
                "context": {
                    "id": 42,
                    "auxData": {"frameId": "IFRAME1", "isDefault": True},
                }
            }
        )
        fm.handle_execution_context_destroyed({"executionContextId": 42})
        fm.select_frame_by_url("myapp.example.com")
        assert fm.get_selected_execution_context_id() is None

    def test_only_default_context_assigned(self, fm: FrameManager) -> None:
        """Non-default contexts should not be assigned to the frame."""
        fm.handle_execution_context_created(
            {
                "context": {
                    "id": 99,
                    "auxData": {"frameId": "IFRAME1", "isDefault": False},
                }
            }
        )
        fm.select_frame_by_url("myapp.example.com")
        assert fm.get_selected_execution_context_id() is None


class TestEventBuffer:
    """Tests for frame event buffering."""

    def test_events_buffered(self, fm: FrameManager) -> None:
        """Frame events should be buffered."""
        fm.handle_frame_attached({"frameId": "F1", "parentFrameId": "MAIN"})
        fm.handle_frame_navigated({"frame": {"id": "F1", "url": "https://test.com"}})
        events = fm.drain_events()
        assert len(events) == 2
        assert events[0]["type"] == "attached"
        assert events[1]["type"] == "navigated"

    def test_drain_clears_buffer(self, fm: FrameManager) -> None:
        """drain_events should clear the buffer."""
        fm.handle_frame_attached({"frameId": "F1", "parentFrameId": "MAIN"})
        fm.drain_events()
        assert fm.drain_events() == []

    def test_event_has_timestamp(self, fm: FrameManager) -> None:
        """Events should have a timestamp."""
        fm.handle_frame_attached({"frameId": "F1", "parentFrameId": "MAIN"})
        events = fm.drain_events()
        assert "timestamp" in events[0]
        assert isinstance(events[0]["timestamp"], float)
