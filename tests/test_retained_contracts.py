"""Contracts that outlive the MCP front.

Four pins proved only by tests that go with the front (#92), moved here first so
the deletion is a deletion and nothing else (#91). Each one covers a module that
stays: ``tool_registry``, ``cdp_handler`` and ``mcp_response``.

Every test here is a pin. It must fail if the thing it pins changes. No Chrome,
no daemon subprocess, no Node -- direct imports and literals only.

- The exact ``CDP_TOOLS`` set, and that it still holds the frame and element
  tools. Was ``test_mcp_surface_contract.TestDerivedSets.test_cdp_tools`` and
  ``test_inspect_mode.TestToolSetCompleteness``.
- The ``_CDP_HANDLERS`` table and its parity with ``CDP_TOOLS``. The parity
  assertion also runs at import time inside ``cdp_handler``; this pins the table
  itself, so a matched pair of edits to both sides still goes red. Was
  ``test_mcp_surface_contract.TestCdpHandlers``.
- The ``mcp_response`` builders and ``extract_text_items``. Was
  ``test_mcp_surface_contract.TestMcpResponseBuilders``.
"""

from __future__ import annotations

from typing import Any

from browser_tools import cdp_handler, mcp_response, tool_registry
from browser_tools.mcp_response import extract_text_items
from browser_tools.tool_registry import CDP_TOOLS


class TestCdpToolSet:
    """The routed tool set is a pinned literal, and it is complete."""

    def test_cdp_tools(self) -> None:
        assert frozenset(
            {
                "ax_find",
                "ax_node",
                "element_exists",
                "element_visible",
                "export_pdf",
                "get_attr",
                "get_frame_events",
                "get_frame_storage",
                "get_html",
                "get_text",
                "list_frames",
                "reset_frame",
                "screencast_start",
                "screencast_stop",
                "screenshot_element",
                "select_frame",
                "wait_idle",
                "wait_stable",
            }
        ) == tool_registry.CDP_TOOLS

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


class TestCdpHandlers:
    """_CDP_HANDLERS literal and parity with CDP_TOOLS."""

    EXPECTED: dict[str, str] = {  # noqa: RUF012
        "list_frames": "_handle_list_frames",
        "select_frame": "_handle_select_frame",
        "reset_frame": "_handle_reset_frame",
        "get_frame_events": "_handle_get_frame_events",
        "get_frame_storage": "_handle_get_frame_storage",
        "ax_find": "_handle_ax_find",
        "ax_node": "_handle_ax_node",
        "export_pdf": "_handle_export_pdf",
        "screenshot_element": "_handle_screenshot_element",
        "screencast_start": "_handle_screencast_start",
        "screencast_stop": "_handle_screencast_stop",
        "wait_idle": "_handle_wait_idle",
        "wait_stable": "_handle_wait_stable",
        "get_text": "_handle_get_text",
        "get_html": "_handle_get_html",
        "get_attr": "_handle_get_attr",
        "element_exists": "_handle_element_exists",
        "element_visible": "_handle_element_visible",
    }

    def test_exact(self) -> None:
        assert cdp_handler._CDP_HANDLERS == self.EXPECTED

    def test_length(self) -> None:
        assert len(cdp_handler._CDP_HANDLERS) == 18

    def test_parity_with_cdp_tools(self) -> None:
        assert set(cdp_handler._CDP_HANDLERS) == set(tool_registry.CDP_TOOLS)

    def test_values_are_handler_names(self) -> None:
        for v in cdp_handler._CDP_HANDLERS.values():
            assert v.startswith("_handle_")


class TestMcpResponseBuilders:
    """Pin the four builder functions and extract_text_items shapes."""

    def test_text_response(self) -> None:
        assert mcp_response.text_response("hello") == {
            "result": {"content": [{"type": "text", "text": "hello"}]}
        }

    def test_error_response(self) -> None:
        assert mcp_response.error_response("boom") == {
            "result": {"content": [{"type": "text", "text": "boom"}], "isError": True}
        }

    def test_make_text(self) -> None:
        assert mcp_response.make_text("hi") == {
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": "hi"}]},
            "id": 0,
        }

    def test_make_error(self) -> None:
        assert mcp_response.make_error("oops") == {
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": "Error: oops"}], "isError": True},
            "id": 0,
        }

    def test_extract_bare_legacy(self) -> None:
        resp: dict[str, Any] = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        assert extract_text_items(resp) == ["a", "b"]

    def test_extract_bare_wrapper(self) -> None:
        resp = {"result": {"content": [{"type": "text", "text": "wrapped"}]}}
        assert extract_text_items(resp) == ["wrapped"]

    def test_extract_jsonrpc_framed(self) -> None:
        resp = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "framed"}]}, "id": 7}
        assert extract_text_items(resp) == ["framed"]

    def test_extract_empty_when_no_content(self) -> None:
        assert extract_text_items({}) == []
        assert extract_text_items({"result": {}}) == []

    def test_extract_ignores_non_text(self) -> None:
        resp = {"result": {"content": [{"type": "image", "data": "abc"}, {"type": "text", "text": "ok"}]}}
        assert extract_text_items(resp) == ["ok"]
