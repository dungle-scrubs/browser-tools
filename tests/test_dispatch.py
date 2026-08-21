"""Tests for the data-driven tool dispatcher in ``mcp_daemon``.

The dispatcher reads routing flags from ``tool_registry`` and routes a tool
call to the CDP handler, the local handler, the screenshot paint-gate, or the
MCP broker - plus the inspect-mode gate and post-navigation interstitial
detection. These tests drive it with fakes so the policy is checked without
spawning processes or real CDP.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from browser_tools.mcp_daemon import DispatchContext, dispatch_tool
from browser_tools.mcp_response import extract_text_items


def _request(
    tool: str, arguments: dict[str, Any] | None = None, client_id: int = 1
) -> dict[str, Any]:
    """Build a tools/call JSON-RPC request like the daemon receives."""
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
        "id": client_id,
    }


class FakeBroker:
    """Records forwarded requests and returns a canned response."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response = response or {
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": "forwarded"}]},
        }

    def request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self.requests.append({"method": method, "params": params, "timeout": timeout})
        return dict(self.response)


class FakeCdpHandler:
    """Records CDP + native calls and serves a configurable interstitial detection."""

    def __init__(self, mode: str = "full", detection: dict[str, Any] | None = None) -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.native_calls: list[tuple[str, dict[str, Any]]] = []
        self.navigation_marks = 0
        self.detection = detection
        self.detection_runs = 0

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "cdp-ok"}]}}

    def call_native(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.native_calls.append((name, args))
        return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "native-ok"}]}}

    def mark_native_navigation(self) -> None:
        self.navigation_marks += 1

    def run_post_navigation_detection(self) -> dict[str, Any] | None:
        self.detection_runs += 1
        return self.detection

    def await_paint_ready(self, timeout_ms: int = 0) -> bool:
        return True


def _ctx(
    broker: FakeBroker | None = None,
    cdp: FakeCdpHandler | None = None,
    engine: str = "native",
) -> DispatchContext:
    return DispatchContext(broker or FakeBroker(), cdp or FakeCdpHandler(), engine)  # type: ignore[arg-type]


def _text(response: dict[str, Any]) -> str:
    return "".join(extract_text_items(response))


# --------------------------------------------------------------------------- #
# Inspect-mode gate
# --------------------------------------------------------------------------- #


def test_inspect_gate_refuses_blocked_tool() -> None:
    broker = FakeBroker()
    cdp = FakeCdpHandler(mode="inspect")
    response = dispatch_tool(_request("click"), 7, _ctx(broker, cdp))
    assert response["id"] == 7
    assert "E004" in _text(response)
    assert "blocked in inspect mode" in _text(response)
    assert broker.requests == []
    assert cdp.calls == []


def test_inspect_gate_allows_observation_tool() -> None:
    broker = FakeBroker()
    cdp = FakeCdpHandler(mode="inspect")
    response = dispatch_tool(_request("take_snapshot"), 1, _ctx(broker, cdp))
    # take_snapshot is an observation tool; it is allowed in inspect mode. Under
    # the native default it now routes to the native backend, not the broker.
    assert cdp.native_calls == [("take_snapshot", {})]
    assert broker.requests == []
    assert response["id"] == 1


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_cdp_tool_routes_to_cdp_handler() -> None:
    cdp = FakeCdpHandler()
    response = dispatch_tool(_request("list_frames", {"depth": 2}), 3, _ctx(cdp=cdp))
    assert cdp.calls == [("list_frames", {"depth": 2})]
    assert response["id"] == 3
    assert _text(response) == "cdp-ok"


def test_screenshot_gate_routes_to_paint_gate() -> None:
    canned = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "shot"}]}}
    with patch(
        "browser_tools.mcp_daemon._take_screenshot_with_paint_gate", return_value=canned
    ) as mock_gate:
        broker = FakeBroker()
        cdp = FakeCdpHandler()
        request = _request("take_screenshot")
        response = dispatch_tool(request, 9, _ctx(broker, cdp))
    mock_gate.assert_called_once_with(request, 9, broker, cdp)
    assert _text(response) == "shot"


def test_default_tool_forwards_to_broker() -> None:
    broker = FakeBroker()
    request = _request("evaluate_script", {"expression": "1+1"})
    response = dispatch_tool(request, 2, _ctx(broker=broker))
    assert len(broker.requests) == 1
    assert broker.requests[0]["method"] == "tools/call"
    assert broker.requests[0]["params"] == request["params"]
    assert response["id"] == 2


# --------------------------------------------------------------------------- #
# Engine flip: native backend by default; --engine mcp keeps the Node path
# --------------------------------------------------------------------------- #


def test_take_snapshot_routes_to_native_backend_by_default() -> None:
    broker = FakeBroker()
    cdp = FakeCdpHandler()
    response = dispatch_tool(_request("take_snapshot", {"compact": True}), 5, _ctx(broker, cdp))
    assert cdp.native_calls == [("take_snapshot", {"compact": True})]
    assert broker.requests == []
    assert response["id"] == 5
    assert _text(response) == "native-ok"


def test_click_and_fill_route_to_native_backend_by_default() -> None:
    for tool, args in (("click", {"uid": "1-2"}), ("fill", {"uid": "1-3", "value": "x"})):
        broker = FakeBroker()
        cdp = FakeCdpHandler()
        dispatch_tool(_request(tool, args), 1, _ctx(broker, cdp))
        assert cdp.native_calls == [(tool, args)]
        assert broker.requests == []


def test_native_tools_forward_to_broker_under_engine_mcp() -> None:
    for tool, args in (
        ("take_snapshot", {"compact": True}),
        ("click", {"uid": "1-2"}),
        ("fill", {"uid": "1-3", "value": "x"}),
    ):
        broker = FakeBroker()
        cdp = FakeCdpHandler()
        request = _request(tool, args)
        response = dispatch_tool(request, 2, _ctx(broker, cdp, engine="mcp"))
        # --engine mcp: the Node broker gets the call, unchanged; native untouched.
        assert cdp.native_calls == []
        assert len(broker.requests) == 1
        assert broker.requests[0]["params"] == request["params"]
        assert response["id"] == 2


def test_native_navigation_invalidates_snapshot_uids() -> None:
    cdp = FakeCdpHandler()
    dispatch_tool(_request("navigate_page", {"url": "https://x"}), 1, _ctx(cdp=cdp))
    assert cdp.navigation_marks == 1


def test_navigation_does_not_mark_native_under_engine_mcp() -> None:
    cdp = FakeCdpHandler()
    dispatch_tool(_request("navigate_page", {"url": "https://x"}), 1, _ctx(cdp=cdp, engine="mcp"))
    assert cdp.navigation_marks == 0


# --------------------------------------------------------------------------- #
# Post-navigation interstitial detection
# --------------------------------------------------------------------------- #


def test_navigation_triggers_detection_warning() -> None:
    cdp = FakeCdpHandler(
        detection={"detections": [{"type": "cloudflare"}], "auto_retried": False, "retries_used": 0}
    )
    with patch("browser_tools.mcp_daemon.format_interstitials", return_value="⚠️ CHALLENGE"):
        response = dispatch_tool(_request("navigate_page", {"url": "https://x"}), 1, _ctx(cdp=cdp))
    assert cdp.detection_runs == 1
    assert "⚠️ CHALLENGE" in _text(response)


def test_navigation_auto_cleared_appends_note() -> None:
    cdp = FakeCdpHandler(detection={"auto_retried": True, "retries_used": 2})
    response = dispatch_tool(_request("new_page", {"url": "https://y"}), 1, _ctx(cdp=cdp))
    text = _text(response)
    assert "auto-cleared" in text
    assert "2" in text


def test_non_navigation_tool_skips_detection() -> None:
    cdp = FakeCdpHandler(detection={"detections": [{"type": "cloudflare"}]})
    dispatch_tool(_request("take_snapshot"), 1, _ctx(cdp=cdp))
    assert cdp.detection_runs == 0


def test_navigation_skips_detection_on_error_response() -> None:
    broker = FakeBroker(response={"jsonrpc": "2.0", "error": {"code": -1, "message": "boom"}})
    cdp = FakeCdpHandler(detection={"detections": [{"type": "cloudflare"}]})
    dispatch_tool(_request("navigate_page"), 1, _ctx(broker=broker, cdp=cdp))
    assert cdp.detection_runs == 0
