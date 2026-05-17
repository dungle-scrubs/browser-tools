"""Unit tests for the 11 new Rodney-integration tools.

Tests cover: ax_find, ax_node, wait_idle, wait_stable, get_text, get_html,
get_attr, export_pdf, screenshot_element, element_exists, element_visible.

All tests use mock CDPClient instances — no real browser required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import the daemon class under test
from mcp_daemon import CDPHandler as BrowserCDPHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cdp_handler(mock_cdp: Any) -> BrowserCDPHandler:
    """Create a BrowserCDPHandler with a mocked CDPClient.

    Args:
        mock_cdp: Mocked CDPClient with connected=True.

    Returns:
        BrowserCDPHandler with the mock client injected.
    """
    handler = BrowserCDPHandler.__new__(BrowserCDPHandler)
    handler._cdp_client = mock_cdp
    handler._frame_manager = MagicMock()
    handler._loop = None
    handler._stop_event = None
    handler._mode = "full"
    return handler


def connected_cdp(send_return: dict | None = None) -> MagicMock:
    """Create a mock CDPClient that is connected with a configured send() response.

    Args:
        send_return: Value returned by cdp.send(). Defaults to empty dict.

    Returns:
        MagicMock with connected=True and async send().
    """
    cdp = MagicMock()
    cdp.connected = True
    cdp.send = AsyncMock(return_value=send_return or {})
    return cdp


def disconnected_cdp() -> MagicMock:
    """Create a mock CDPClient that is disconnected.

    Returns:
        MagicMock with connected=False.
    """
    cdp = MagicMock()
    cdp.connected = False
    return cdp


# ---------------------------------------------------------------------------
# ax_find tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ax_find_returns_nodes_by_role():
    """ax_find with role returns formatted node list."""
    nodes_response = {
        "nodes": [
            {
                "nodeId": "42",
                "backendDOMNodeId": 100,
                "role": {"value": "button"},
                "name": {"value": "Submit"},
                "ignored": False,
                "properties": [{"name": "disabled", "value": {"value": False}}],
            }
        ]
    }
    cdp = connected_cdp(nodes_response)
    handler = make_cdp_handler(cdp)

    result = await handler._handle_ax_find({"role": "button"})

    cdp.send.assert_called_once_with("Accessibility.queryAXTree", {"role": "button"})
    text = result["result"]["content"][0]["text"]
    assert "button" in text
    assert "Submit" in text
    assert "1 node" in text


@pytest.mark.asyncio
async def test_ax_find_by_name():
    """ax_find with name uses accessibleName param."""
    cdp = connected_cdp({"nodes": []})
    handler = make_cdp_handler(cdp)

    await handler._handle_ax_find({"name": "Login"})

    cdp.send.assert_called_once_with("Accessibility.queryAXTree", {"accessibleName": "Login"})


@pytest.mark.asyncio
async def test_ax_find_combined_role_and_name():
    """ax_find with both role and name sends both params."""
    cdp = connected_cdp({"nodes": []})
    handler = make_cdp_handler(cdp)

    await handler._handle_ax_find({"role": "link", "name": "Home"})

    cdp.send.assert_called_once_with(
        "Accessibility.queryAXTree", {"role": "link", "accessibleName": "Home"}
    )


@pytest.mark.asyncio
async def test_ax_find_e007_neither_role_nor_name():
    """ax_find with no role or name returns E007 validation error."""
    cdp = connected_cdp()
    handler = make_cdp_handler(cdp)

    result = await handler._handle_ax_find({})

    text = result["result"]["content"][0]["text"]
    assert "E007" in text
    cdp.send.assert_not_called()


@pytest.mark.asyncio
async def test_ax_find_empty_results():
    """ax_find with no matches returns clear message, not an error."""
    cdp = connected_cdp({"nodes": []})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_ax_find({"role": "button"})

    text = result["result"]["content"][0]["text"]
    assert "No accessibility nodes found" in text


@pytest.mark.asyncio
async def test_ax_find_ignores_ignored_nodes():
    """ax_find filters out nodes with ignored=True."""
    cdp = connected_cdp(
        {
            "nodes": [
                {
                    "nodeId": "1",
                    "role": {"value": "button"},
                    "name": {"value": "Visible"},
                    "ignored": False,
                    "properties": [],
                },
                {
                    "nodeId": "2",
                    "role": {"value": "button"},
                    "name": {"value": "Hidden"},
                    "ignored": True,
                    "properties": [],
                },
            ]
        }
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_ax_find({"role": "button"})
    text = result["result"]["content"][0]["text"]
    assert "1 node" in text
    assert "Visible" in text
    assert "Hidden" not in text


@pytest.mark.asyncio
async def test_ax_find_disconnected_cdp():
    """ax_find returns error when CDP not connected."""
    handler = make_cdp_handler(disconnected_cdp())
    result = await handler._handle_ax_find({"role": "button"})
    assert (
        "error" in result["result"]["content"][0]["text"].lower()
        or "not connected" in result["result"]["content"][0]["text"].lower()
    )


# ---------------------------------------------------------------------------
# ax_node tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ax_node_returns_properties():
    """ax_node returns role, name, and properties for found element."""
    cdp = connected_cdp()
    # Runtime.evaluate → returns objectId
    # DOM.requestNode → returns nodeId
    # Accessibility.getPartialAXTree → returns nodes
    cdp.send = AsyncMock(
        side_effect=[
            {"result": {"type": "object", "objectId": "obj-1"}},  # Runtime.evaluate
            {"nodeId": 42},  # DOM.requestNode
            {
                "nodes": [
                    {
                        "backendDOMNodeId": 42,
                        "role": {"value": "button"},
                        "name": {"value": "Submit"},
                        "description": {"value": ""},
                        "ignored": False,
                        "properties": [
                            {"name": "disabled", "value": {"value": False}},
                            {"name": "required", "value": {"value": True}},
                        ],
                    }
                ]
            },  # Accessibility.getPartialAXTree
        ]
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_ax_node({"selector": "#submit"})
    text = result["result"]["content"][0]["text"]

    assert "button" in text
    assert "Submit" in text
    assert "disabled" in text
    assert "required" in text


@pytest.mark.asyncio
async def test_ax_node_e006_selector_not_found():
    """ax_node returns E006 when selector matches nothing."""
    cdp = connected_cdp()
    cdp.send = AsyncMock(return_value={"result": {"type": "undefined", "subtype": "null"}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_ax_node({"selector": "#nonexistent"})
    text = result["result"]["content"][0]["text"]
    assert "E006" in text


@pytest.mark.asyncio
async def test_ax_node_missing_selector():
    """ax_node without selector returns validation error."""
    handler = make_cdp_handler(connected_cdp())
    result = await handler._handle_ax_node({})
    assert "required" in result["result"]["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# wait_idle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_idle_success():
    """wait_idle returns success message on resolve."""
    cdp = connected_cdp(
        {
            "result": {"type": "string", "value": "idle after 200ms"},
            "exceptionDetails": None,
        }
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_wait_idle({})
    text = result["result"]["content"][0]["text"]
    assert "idle" in text.lower()


@pytest.mark.asyncio
async def test_wait_idle_timeout():
    """wait_idle returns E008 on timeout."""
    cdp = connected_cdp(
        {
            "result": {"type": "string", "value": ""},
            "exceptionDetails": {
                "exception": {"description": "E008: wait_idle timed out after 5000ms"}
            },
        }
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_wait_idle({"timeout_ms": 5000})
    text = result["result"]["content"][0]["text"]
    assert "E008" in text


@pytest.mark.asyncio
async def test_wait_idle_uses_custom_params():
    """wait_idle passes timeout_ms and idle_ms into the JS."""
    cdp = connected_cdp(
        {
            "result": {"type": "string", "value": "idle after 100ms"},
        }
    )
    handler = make_cdp_handler(cdp)

    await handler._handle_wait_idle({"timeout_ms": 3000, "idle_ms": 200})

    call_args = cdp.send.call_args
    assert call_args[0][0] == "Runtime.evaluate"
    js = call_args[0][1]["expression"]
    assert "3000" in js
    assert "200" in js


# ---------------------------------------------------------------------------
# wait_stable tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_stable_success():
    """wait_stable returns success message on resolve."""
    cdp = connected_cdp(
        {
            "result": {"type": "string", "value": "stable after 300ms"},
        }
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_wait_stable({})
    text = result["result"]["content"][0]["text"]
    assert "stable" in text.lower()


@pytest.mark.asyncio
async def test_wait_stable_timeout():
    """wait_stable returns E008 on timeout."""
    cdp = connected_cdp(
        {
            "result": {"type": "string", "value": ""},
            "exceptionDetails": {
                "exception": {"description": "E008: wait_stable timed out after 5000ms"}
            },
        }
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_wait_stable({})
    text = result["result"]["content"][0]["text"]
    assert "E008" in text


@pytest.mark.asyncio
async def test_wait_stable_cleanup_called():
    """wait_stable JS includes cleanup in both success and timeout paths."""
    cdp = connected_cdp(
        {
            "result": {"type": "string", "value": "stable after 100ms"},
        }
    )
    handler = make_cdp_handler(cdp)

    await handler._handle_wait_stable({"stable_ms": 100})
    js = cdp.send.call_args[0][1]["expression"]
    # Verify cleanup pattern is present
    assert "observer.disconnect" in js
    assert "MutationObserver" in js


# ---------------------------------------------------------------------------
# get_text tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_text_returns_text_content():
    """get_text returns element's textContent."""
    cdp = connected_cdp({"result": {"type": "string", "value": "Hello world"}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_text({"selector": "h1"})
    assert result["result"]["content"][0]["text"] == "Hello world"


@pytest.mark.asyncio
async def test_get_text_e006_not_found():
    """get_text returns E006 when selector matches nothing."""
    # null return from querySelector
    cdp = connected_cdp({"result": {"type": "object", "subtype": "null", "value": None}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_text({"selector": "#missing"})
    text = result["result"]["content"][0]["text"]
    assert "E006" in text


# ---------------------------------------------------------------------------
# get_html tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_html_returns_outer_html():
    """get_html returns element's outerHTML."""
    cdp = connected_cdp({"result": {"type": "string", "value": "<div class='x'>content</div>"}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_html({"selector": ".x"})
    assert "<div" in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_html_e006_not_found():
    """get_html returns E006 when selector matches nothing."""
    cdp = connected_cdp({"result": {"type": "object", "subtype": "null", "value": None}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_html({"selector": "#missing"})
    assert "E006" in result["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# get_attr tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_attr_returns_value():
    """get_attr returns attribute value."""
    cdp = connected_cdp({"result": {"type": "string", "value": "https://example.com"}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_attr({"selector": "a", "attribute": "href"})
    assert "https://example.com" in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_attr_null_when_absent():
    """get_attr returns null (not error) when element exists but attribute absent."""
    cdp = connected_cdp({"result": {"type": "string", "value": "__ATTR_NULL__"}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_attr({"selector": "div", "attribute": "data-missing"})
    text = result["result"]["content"][0]["text"]
    assert "null" in text


@pytest.mark.asyncio
async def test_get_attr_e006_no_element():
    """get_attr returns E006 when element not found."""
    cdp = connected_cdp({"result": {"type": "string", "value": "__ELEMENT_NOT_FOUND__"}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_get_attr({"selector": "#missing", "attribute": "href"})
    assert "E006" in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_attr_missing_attribute_param():
    """get_attr without attribute param returns validation error."""
    handler = make_cdp_handler(connected_cdp())
    result = await handler._handle_get_attr({"selector": "a"})
    assert "required" in result["result"]["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# export_pdf tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_pdf_writes_file(tmp_path):
    """export_pdf writes decoded base64 PDF to disk."""
    import base64

    fake_pdf = b"%PDF-1.4 fake content"
    encoded = base64.b64encode(fake_pdf).decode()

    cdp = connected_cdp({"data": encoded})
    handler = make_cdp_handler(cdp)

    out = tmp_path / "test.pdf"
    result = await handler._handle_export_pdf({"path": str(out)})

    assert out.exists()
    assert out.read_bytes() == fake_pdf
    text = result["result"]["content"][0]["text"]
    assert "PDF saved" in text
    assert str(out) in text


@pytest.mark.asyncio
async def test_export_pdf_default_filename(tmp_path, monkeypatch):
    """export_pdf uses timestamp default filename when no path given."""
    import base64

    encoded = base64.b64encode(b"%PDF-fake").decode()
    cdp = connected_cdp({"data": encoded})
    handler = make_cdp_handler(cdp)

    # monkeypatch cwd to tmp_path
    monkeypatch.chdir(tmp_path)
    result = await handler._handle_export_pdf({})

    text = result["result"]["content"][0]["text"]
    assert "_page.pdf" in text


@pytest.mark.asyncio
async def test_export_pdf_e009_write_failure():
    """export_pdf returns E009 on write failure."""
    import base64

    encoded = base64.b64encode(b"%PDF-fake").decode()
    cdp = connected_cdp({"data": encoded})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_export_pdf({"path": "/nonexistent/dir/test.pdf"})
    text = result["result"]["content"][0]["text"]
    assert "E009" in text


# ---------------------------------------------------------------------------
# screenshot_element tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_element_captures_and_returns_base64():
    """screenshot_element returns base64 image data."""
    import base64

    fake_png = base64.b64encode(b"\x89PNG fake").decode()
    cdp = connected_cdp()
    cdp.send = AsyncMock(
        side_effect=[
            # Runtime.evaluate for getBoundingClientRect
            {"result": {"type": "object", "value": {"x": 10, "y": 20, "width": 100, "height": 50}}},
            # Page.captureScreenshot
            {"data": fake_png},
        ]
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_screenshot_element({"selector": ".chart"})
    text = result["result"]["content"][0]["text"]
    assert "data:image/png;base64," in text


@pytest.mark.asyncio
async def test_screenshot_element_writes_file(tmp_path):
    """screenshot_element writes PNG to path when provided."""
    import base64

    fake_png = base64.b64encode(b"\x89PNG fake").decode()

    cdp = connected_cdp()
    cdp.send = AsyncMock(
        side_effect=[
            {"result": {"type": "object", "value": {"x": 0, "y": 0, "width": 200, "height": 100}}},
            {"data": fake_png},
        ]
    )
    handler = make_cdp_handler(cdp)

    out = tmp_path / "el.png"
    result = await handler._handle_screenshot_element({"selector": "#btn", "path": str(out)})

    assert out.exists()
    text = result["result"]["content"][0]["text"]
    assert "saved to" in text


@pytest.mark.asyncio
async def test_screenshot_element_e006_not_found():
    """screenshot_element returns E006 when selector not found."""
    cdp = connected_cdp({"result": {"type": "object", "value": None}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_screenshot_element({"selector": "#missing"})
    assert "E006" in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_screenshot_element_scrolls_into_view():
    """screenshot_element JS includes scrollIntoView call."""
    import base64

    fake_png = base64.b64encode(b"\x89PNG").decode()
    cdp = connected_cdp()
    cdp.send = AsyncMock(
        side_effect=[
            {"result": {"type": "object", "value": {"x": 0, "y": 0, "width": 50, "height": 50}}},
            {"data": fake_png},
        ]
    )
    handler = make_cdp_handler(cdp)

    await handler._handle_screenshot_element({"selector": ".el"})
    js = cdp.send.call_args_list[0][0][1]["expression"]
    assert "scrollIntoView" in js


# ---------------------------------------------------------------------------
# element_exists tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_element_exists_found():
    """element_exists returns exists=true and count when elements found."""
    cdp = connected_cdp({"result": {"type": "number", "value": 3}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_element_exists({"selector": "li"})
    text = result["result"]["content"][0]["text"]
    assert '"exists": true' in text
    assert '"count": 3' in text


@pytest.mark.asyncio
async def test_element_exists_not_found():
    """element_exists returns exists=false (not an error) when no matches."""
    cdp = connected_cdp({"result": {"type": "number", "value": 0}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_element_exists({"selector": "#missing"})
    text = result["result"]["content"][0]["text"]
    assert '"exists": false' in text
    assert '"count": 0' in text
    # Must not be an error response
    assert "E006" not in text


@pytest.mark.asyncio
async def test_element_exists_invalid_selector():
    """element_exists returns error for invalid CSS selector."""
    cdp = connected_cdp(
        {
            "result": {},
            "exceptionDetails": {"exception": {"description": "SyntaxError: invalid selector"}},
        }
    )
    handler = make_cdp_handler(cdp)

    result = await handler._handle_element_exists({"selector": "!!invalid!!"})
    text = result["result"]["content"][0]["text"]
    assert "Invalid selector" in text or "invalid" in text.lower()


# ---------------------------------------------------------------------------
# element_visible tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_element_visible_true():
    """element_visible returns visible=true for rendered element."""
    cdp = connected_cdp({"result": {"type": "boolean", "value": True}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_element_visible({"selector": "#btn"})
    assert '"visible": true' in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_element_visible_false_display_none():
    """element_visible returns visible=false for display:none element."""
    cdp = connected_cdp({"result": {"type": "boolean", "value": False}})
    handler = make_cdp_handler(cdp)

    result = await handler._handle_element_visible({"selector": ".hidden"})
    assert '"visible": false' in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_element_visible_checks_css_properties():
    """element_visible JS checks display, visibility, opacity, and rect."""
    cdp = connected_cdp({"result": {"type": "boolean", "value": True}})
    handler = make_cdp_handler(cdp)

    await handler._handle_element_visible({"selector": ".el"})
    js = cdp.send.call_args[0][1]["expression"]
    assert "display" in js
    assert "visibility" in js
    assert "opacity" in js
    assert "getBoundingClientRect" in js


# ---------------------------------------------------------------------------
# Routing — all 11 new tools in CDP_TOOLS frozenset
# ---------------------------------------------------------------------------


def test_all_new_tools_in_cdp_tools():
    """All 11 new tools are registered in CDP_TOOLS frozenset."""
    from mcp_daemon import CDP_TOOLS

    expected = {
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
    missing = expected - CDP_TOOLS
    assert not missing, f"Missing from CDP_TOOLS: {missing}"


def test_no_new_tools_in_local_tools():
    """New tools are NOT in LOCAL_TOOLS (they need CDP/async, not sync local)."""
    from mcp_daemon import LOCAL_TOOLS

    new_tools = {
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
    overlap = new_tools & LOCAL_TOOLS
    assert not overlap, f"New tools incorrectly in LOCAL_TOOLS: {overlap}"


def test_all_new_tools_in_chrome_config():
    """All 11 new tools have TOOL_SCHEMAS entries in chrome_config.py."""
    from browser_tools.chrome_config import TOOL_SCHEMAS

    expected = {
        "ax_find",
        "ax_node",
        "wait_idle",
        "wait_stable",
        "get_text",
        "get_html",
        "get_attr",
        "export_pdf",
        "screenshot_element",
        "element_exists",
        "element_visible",
    }
    missing = expected - set(TOOL_SCHEMAS.keys())
    assert not missing, f"Missing from TOOL_SCHEMAS: {missing}"
