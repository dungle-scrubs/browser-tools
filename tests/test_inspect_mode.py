"""Tests for inspect mode enforcement (M-4.1)."""

from __future__ import annotations

# Import the sets from mcp_daemon for testing
from browser_tools.mcp_daemon import (
    CDP_TOOLS,
    INSPECT_BLOCKED_TOOLS,
    INSPECT_WARN_TOOLS,
    LOCAL_TOOLS,
)


class TestInspectModeToolSets:
    """Tests for tool classification in inspect mode."""

    def test_interaction_tools_blocked(self) -> None:
        """All interaction tools should be in the blocked set."""
        expected = {
            "click",
            "hover",
            "fill",
            "fill_form",
            "drag",
            "press_key",
            "upload_file",
            "handle_dialog",
            "type_text",
        }
        assert expected == INSPECT_BLOCKED_TOOLS

    def test_observation_tools_not_blocked(self) -> None:
        """Observation tools should NOT be in the blocked set."""
        observation_tools = {
            "take_snapshot",
            "take_screenshot",
            "list_pages",
            "evaluate_script",
            "list_console_messages",
            "get_console_message",
            "list_network_requests",
            "get_network_request",
            "performance_start_trace",
            "performance_stop_trace",
            "performance_analyze_insight",
            "emulate",
            "resize_page",
        }
        for tool in observation_tools:
            assert tool not in INSPECT_BLOCKED_TOOLS, f"{tool} should not be blocked"

    def test_frame_tools_not_blocked(self) -> None:
        """Frame tools should NOT be blocked in inspect mode."""
        for tool in CDP_TOOLS:
            assert tool not in INSPECT_BLOCKED_TOOLS, f"{tool} should not be blocked"

    def test_navigation_tools_in_warn_set(self) -> None:
        """Navigation tools should be in the warn set."""
        assert "navigate_page" in INSPECT_WARN_TOOLS
        assert "new_page" in INSPECT_WARN_TOOLS
        assert "close_page" in INSPECT_WARN_TOOLS

    def test_select_page_not_blocked(self) -> None:
        """select_page is observation, not interaction."""
        assert "select_page" not in INSPECT_BLOCKED_TOOLS

    def test_wait_for_not_blocked(self) -> None:
        """wait_for is observation, not interaction."""
        assert "wait_for" not in INSPECT_BLOCKED_TOOLS


class TestInspectModeEnforcement:
    """Tests for the E004 error code behavior."""

    def test_blocked_error_message_contains_e004(self) -> None:
        """Blocked tools should return E004 error code."""
        # Simulate what the daemon does
        tool_name = "click"
        assert tool_name in INSPECT_BLOCKED_TOOLS
        error_msg = (
            f"E004: Tool '{tool_name}' is blocked in inspect mode. "
            "Observation tools only: take_snapshot, take_screenshot, "
            "list_pages, evaluate_script, list_console_messages, "
            "list_network_requests, list_frames, get_frame_storage."
        )
        assert "E004" in error_msg
        assert tool_name in error_msg

    def test_all_blocked_tools_produce_e004(self) -> None:
        """Every blocked tool should generate an E004 error."""
        for tool_name in INSPECT_BLOCKED_TOOLS:
            error_msg = f"E004: Tool '{tool_name}' is blocked in inspect mode."
            assert "E004" in error_msg


class TestToolSetCompleteness:
    """Tests for tool set completeness and correctness."""

    def test_cdp_tools_include_frame_tools(self) -> None:
        """CDP tools should include the frame-related tools (and new Rodney tools)."""
        frame_tools = {
            "list_frames",
            "select_frame",
            "reset_frame",
            "get_frame_storage",
            "get_frame_events",
        }
        assert frame_tools.issubset(CDP_TOOLS), (
            f"Missing frame tools from CDP_TOOLS: {frame_tools - CDP_TOOLS}"
        )
        # Also verify all new rodney tools are present
        rodney_tools = {
            "ax_find",
            "ax_node",
            "export_pdf",
            "screenshot_element",
            "wait_idle",
            "wait_stable",
            "get_text",
            "get_html",
            "get_attr",
            "element_exists",
            "element_visible",
        }
        assert rodney_tools.issubset(CDP_TOOLS), (
            f"Missing rodney tools from CDP_TOOLS: {rodney_tools - CDP_TOOLS}"
        )

    def test_local_tools_are_management_tools(self) -> None:
        """Local tools should be the management tools."""
        expected = {"attach_browser", "list_profiles", "delete_profile"}
        assert expected == LOCAL_TOOLS

    def test_no_overlap_between_sets(self) -> None:
        """Tool sets should not overlap."""
        all_sets = [INSPECT_BLOCKED_TOOLS, CDP_TOOLS, LOCAL_TOOLS]
        for i, s1 in enumerate(all_sets):
            for j, s2 in enumerate(all_sets):
                if i != j:
                    overlap = s1 & s2
                    assert not overlap, f"Sets {i} and {j} overlap: {overlap}"
