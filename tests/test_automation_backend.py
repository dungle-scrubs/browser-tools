"""Unit tests for the AutomationBackend seam (Chrome + Camoufox adapters).

The seam lets the Camoufox translation be exercised through one interface with
a fake session, instead of driving the whole browser_session.call_tool router
or a real Camoufox browser (the only thing that covered this path before was
the e2e suite, which is excluded from CI unit runs).
"""

from __future__ import annotations

from typing import Any

from browser_tools.automation_backend import (
    CAMOUFOX_TOOL_MAP,
    CamoufoxBackend,
    ChromeBackend,
)
from browser_tools.mcp_response import extract_text_items


class FakeCamoufoxSession:
    """Records call_tool invocations and returns a canned result."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result if result is not None else {"result": {"ok": True}}

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((tool, dict(args or {})))
        return self._result


class FakeController:
    """Records invoke_tool invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        return {"result": {"content": [{"type": "text", "text": "chrome-ok"}]}}


def _text(response: dict[str, Any]) -> str:
    return "".join(extract_text_items(response))


# --------------------------------------------------------------------------- #
# CamoufoxBackend: mapped tools (name + arg translation)
# --------------------------------------------------------------------------- #


def test_mapped_tool_translates_name_and_args() -> None:
    """navigate_page maps to 'navigate' with translated args."""
    session = FakeCamoufoxSession()
    CamoufoxBackend(session).invoke("navigate_page", {"url": "https://x", "wait_until": "dom"})
    assert session.calls == [("navigate", {"url": "https://x", "wait_until": "dom"})]


def test_new_page_maps_to_navigate_with_default_wait() -> None:
    """new_page is a camoufox automation tool; missing wait_until defaults to load."""
    session = FakeCamoufoxSession()
    CamoufoxBackend(session).invoke("new_page", {"url": "https://y"})
    assert session.calls == [("navigate", {"url": "https://y", "wait_until": "load"})]


def test_click_translates_uid_to_selector() -> None:
    """click translates the browser-tools 'uid' arg to camoufox 'selector'."""
    session = FakeCamoufoxSession()
    CamoufoxBackend(session).invoke("click", {"uid": "btn"})
    assert session.calls == [("click", {"selector": "btn"})]


def test_take_screenshot_translates_filepath_and_fullpage() -> None:
    """take_screenshot translates filePath/fullPage to camoufox path/full_page."""
    session = FakeCamoufoxSession()
    CamoufoxBackend(session).invoke("take_screenshot", {"filePath": "/tmp/s.png", "fullPage": True})
    assert session.calls == [("screenshot", {"path": "/tmp/s.png", "full_page": True})]


def test_type_text_aliases_fill() -> None:
    """type_text maps to fill (alias)."""
    session = FakeCamoufoxSession()
    CamoufoxBackend(session).invoke("type_text", {"uid": "#q", "value": "hi"})
    assert session.calls == [("fill", {"selector": "#q", "value": "hi"})]


def test_mapped_success_result_wrapped_as_mcp_text() -> None:
    """A successful session result is wrapped into an MCP text response."""
    session = FakeCamoufoxSession(result={"result": {"title": "Example"}})
    response = CamoufoxBackend(session).invoke("navigate_page", {"url": "https://x"})
    assert "Example" in _text(response)


def test_mapped_error_result_wrapped_as_error_response() -> None:
    """A session error is wrapped into an MCP error response."""
    session = FakeCamoufoxSession(result={"error": "boom"})
    response = CamoufoxBackend(session).invoke("click", {"uid": "x"})
    assert "boom" in _text(response)


# --------------------------------------------------------------------------- #
# CamoufoxBackend: explicitly unsupported (mapped value None)
# --------------------------------------------------------------------------- #


def test_unsupported_mapped_tool_returns_error_without_calling_session() -> None:
    """wait_for is in the map but mapped to None -> unsupported error, no session call."""
    session = FakeCamoufoxSession()
    response = CamoufoxBackend(session).invoke("wait_for", {})
    assert session.calls == []
    assert "not supported in Camoufox mode" in _text(response)


# --------------------------------------------------------------------------- #
# CamoufoxBackend: passthrough (camoufox-native tools, not in the map)
# --------------------------------------------------------------------------- #


def test_passthrough_tool_is_forwarded_unmapped() -> None:
    """wait_for_human is not in the map -> forwarded as-is, no translation."""
    session = FakeCamoufoxSession()
    CamoufoxBackend(session).invoke("wait_for_human", {"reason": "CAPTCHA"})
    assert session.calls == [("wait_for_human", {"reason": "CAPTCHA"})]


# --------------------------------------------------------------------------- #
# ChromeBackend
# --------------------------------------------------------------------------- #


def test_chrome_backend_delegates_to_controller() -> None:
    """ChromeBackend.invoke delegates straight to controller.invoke_tool."""
    controller = FakeController()
    response = ChromeBackend(controller).invoke(  # type: ignore[arg-type]
        "navigate_page", {"url": "https://x"}
    )
    assert controller.calls == [("navigate_page", {"url": "https://x"})]
    assert response == {"result": {"content": [{"type": "text", "text": "chrome-ok"}]}}


# --------------------------------------------------------------------------- #
# Routing truth
# --------------------------------------------------------------------------- #


def test_camoufox_tool_map_is_the_routing_truth() -> None:
    """The map decides which automation tools camoufox owns and how they translate."""
    assert CAMOUFOX_TOOL_MAP["navigate_page"] == "navigate"
    assert CAMOUFOX_TOOL_MAP["click"] == "click"
    assert CAMOUFOX_TOOL_MAP["new_page"] == "navigate"
    assert CAMOUFOX_TOOL_MAP["wait_for"] is None  # explicitly unsupported
