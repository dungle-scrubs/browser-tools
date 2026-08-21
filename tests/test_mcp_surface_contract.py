"""Contract tests that freeze the browser-tools MCP surface.

Every test here is a pin: it must fail if the thing it pins changes.
These are SCHEMA / CONTRACT tests, not behavioral integration tests.
No Chrome, no daemon subprocess, no Node. Only direct imports, mocks,
and source introspection.

Coverage:
- tool_registry.ToolFlags shape, TOOLS keys + flags, derived sets, __all__
- SESSION_TOOLS, Camoufox-exclusive dispatch, CAMOUFOX_TOOL_MAP
- AutomationBackend Protocol shape
- Session handler arg shapes via source introspection + smoke calls
- Bare envelope response shape for session handlers
- mcp_response builder functions + extract_text_items shapes
- cdp_handler._CDP_HANDLERS table + parity with CDP_TOOLS
- mcp_daemon.dispatch_tool routing (CDP, screenshot gate, default-forward,
  inspect gate, navigation interstitial)
- Literal default-forwarded tool names that appear in Python source
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from typing import Any
from unittest.mock import MagicMock, patch

from browser_tools import (
    automation_backend,
    browser_session,
    cdp_handler,
    mcp_daemon,
    mcp_response,
    tool_registry,
)
from browser_tools.automation_backend import AutomationBackend, CamoufoxBackend, ChromeBackend
from browser_tools.browser_session import SessionDispatchContext, dispatch_session_tool
from browser_tools.mcp_daemon import DispatchContext, dispatch_tool
from browser_tools.mcp_response import extract_text_items

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args_get_keys(func: Any) -> set[str]:
    """Return every string key read via args.get(...) or args[...] in func."""
    src = inspect.getsource(func)
    return set(re.findall(r'args(?:\.get|\[)\(?["\']([\w]+)["\']', src))


def _assert_bare_envelope(resp: dict[str, Any]) -> None:
    """Assert resp matches the bare envelope {result: {content: [{type:text}]}}."""
    assert isinstance(resp, dict), f"response must be dict, got {type(resp)}"
    assert "result" in resp, "missing 'result' key"
    result = resp["result"]
    assert isinstance(result, dict), "'result' must be dict"
    assert "content" in result, "missing 'content' in result"
    content = result["content"]
    assert isinstance(content, list) and len(content) >= 1, "content must be non-empty list"
    for item in content:
        assert isinstance(item, dict)
        assert item.get("type") == "text"
        assert isinstance(item.get("text"), str)
    # isError is optional; when present must be bool
    if "isError" in result:
        assert isinstance(result["isError"], bool)


def _assert_error_envelope(resp: dict[str, Any]) -> None:
    _assert_bare_envelope(resp)
    assert resp["result"].get("isError") is True


def _assert_success_envelope(resp: dict[str, Any]) -> None:
    _assert_bare_envelope(resp)
    assert resp["result"].get("isError") is not True


def _request(tool: str, arguments: dict[str, Any] | None = None, client_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
        "id": client_id,
    }


class FakeBroker:
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
    def __init__(self, mode: str = "full", detection: dict[str, Any] | None = None) -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.detection = detection
        self.detection_runs = 0

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "cdp-ok"}]}}

    def run_post_navigation_detection(self) -> dict[str, Any] | None:
        self.detection_runs += 1
        return self.detection

    def await_paint_ready(self, timeout_ms: int = 0) -> bool:
        return True


class FakeController:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None, headless: bool = False):
        self._responses = responses or {}
        self.headless = headless
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(params)))
        return self._responses.get(name, {"result": {"content": [{"type": "text", "text": "ok"}]}})


class FakeCamoufox:
    def __init__(self, result: dict[str, Any] | None = None):
        self._result = result if result is not None else {"result": {"status": "done"}}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((tool, dict(args or {})))
        return self._result


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class TestToolFlagsDataclass:
    """ToolFlags must remain @dataclass(frozen=True) with 8 bool=False fields.

    Must go red if a field is renamed, added, removed, or default changes.
    """

    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(tool_registry.ToolFlags)
        assert tool_registry.ToolFlags.__dataclass_params__.frozen is True  # type: ignore[attr-defined]

    def test_field_names_and_defaults(self) -> None:
        fields = dataclasses.fields(tool_registry.ToolFlags)
        expected = [
            ("cdp", False),
            ("interaction", False),
            ("inspect_blocked", False),
            ("navigation", False),
            ("inspect_warn", False),
            ("page_selecting", False),
            ("screenshot_gate", False),
            ("single_tab", False),
        ]
        actual = [(f.name, f.default) for f in fields]
        assert actual == expected

    def test_field_count(self) -> None:
        assert len(dataclasses.fields(tool_registry.ToolFlags)) == 8

    def test_all_defaults_are_false(self) -> None:
        inst = tool_registry.ToolFlags()
        for f in dataclasses.fields(tool_registry.ToolFlags):
            assert getattr(inst, f.name) is False


class TestToolsDict:
    """TOOLS keys and per-tool flags are a frozen contract.

    Must go red if a tool is added, removed, renamed, or flags change.
    """

    EXPECTED_KEYS = frozenset(
        {
            "ax_find",
            "ax_node",
            "click",
            "close_page",
            "drag",
            "element_exists",
            "element_visible",
            "export_pdf",
            "fill",
            "fill_form",
            "get_attr",
            "get_frame_events",
            "get_frame_storage",
            "get_html",
            "get_text",
            "handle_dialog",
            "hover",
            "list_frames",
            "navigate_page",
            "new_page",
            "press_key",
            "reset_frame",
            "screencast_start",
            "screencast_stop",
            "screenshot_element",
            "select_frame",
            "select_page",
            "take_screenshot",
            "type_text",
            "upload_file",
            "wait_idle",
            "wait_stable",
        }
    )

    # Hardcoded expected flags per tool. Written literally so a flag change fails.
    EXPECTED_FLAGS: dict[str, tool_registry.ToolFlags] = {  # type: ignore[no-redef]  # noqa: RUF012
        "ax_find": tool_registry.ToolFlags(cdp=True),
        "ax_node": tool_registry.ToolFlags(cdp=True),
        "click": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "close_page": tool_registry.ToolFlags(inspect_warn=True),
        "drag": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "element_exists": tool_registry.ToolFlags(cdp=True),
        "element_visible": tool_registry.ToolFlags(cdp=True),
        "export_pdf": tool_registry.ToolFlags(cdp=True),
        "fill": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "fill_form": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "get_attr": tool_registry.ToolFlags(cdp=True),
        "get_frame_events": tool_registry.ToolFlags(cdp=True),
        "get_frame_storage": tool_registry.ToolFlags(cdp=True),
        "get_html": tool_registry.ToolFlags(cdp=True),
        "get_text": tool_registry.ToolFlags(cdp=True),
        "handle_dialog": tool_registry.ToolFlags(inspect_blocked=True),
        "hover": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "list_frames": tool_registry.ToolFlags(cdp=True),
        "navigate_page": tool_registry.ToolFlags(navigation=True, inspect_warn=True),
        "new_page": tool_registry.ToolFlags(
            navigation=True, inspect_warn=True, page_selecting=True, single_tab=True
        ),
        "press_key": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "reset_frame": tool_registry.ToolFlags(cdp=True),
        "screencast_start": tool_registry.ToolFlags(cdp=True),
        "screencast_stop": tool_registry.ToolFlags(cdp=True),
        "screenshot_element": tool_registry.ToolFlags(cdp=True),
        "select_frame": tool_registry.ToolFlags(cdp=True),
        "select_page": tool_registry.ToolFlags(page_selecting=True),
        "take_screenshot": tool_registry.ToolFlags(screenshot_gate=True),
        "type_text": tool_registry.ToolFlags(inspect_blocked=True),
        "upload_file": tool_registry.ToolFlags(interaction=True, inspect_blocked=True),
        "wait_idle": tool_registry.ToolFlags(cdp=True),
        "wait_stable": tool_registry.ToolFlags(cdp=True),
    }

    def test_keys_exact(self) -> None:
        assert set(tool_registry.TOOLS) == set(self.EXPECTED_KEYS)
        assert len(tool_registry.TOOLS) == 32

    def test_flags_per_tool_exact(self) -> None:
        for name, expected in self.EXPECTED_FLAGS.items():
            assert tool_registry.TOOLS[name] == expected, f"flags drift for {name}"
        # No extra keys beyond the expected map
        assert set(tool_registry.TOOLS) == set(self.EXPECTED_FLAGS)


class TestDerivedSets:
    """Each derived frozenset must equal a hardcoded literal.

    Must go red if a flag → set mapping changes. Do NOT recompute via _names().
    """

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

    def test_interaction_tools(self) -> None:
        assert frozenset(
            {"click", "drag", "fill", "fill_form", "hover", "press_key", "upload_file"}
        ) == tool_registry.INTERACTION_TOOLS

    def test_inspect_blocked_tools(self) -> None:
        assert frozenset(
            {
                "click",
                "drag",
                "fill",
                "fill_form",
                "handle_dialog",
                "hover",
                "press_key",
                "type_text",
                "upload_file",
            }
        ) == tool_registry.INSPECT_BLOCKED_TOOLS

    def test_navigation_tools(self) -> None:
        assert frozenset({"navigate_page", "new_page"}) == tool_registry.NAVIGATION_TOOLS

    def test_inspect_warn_tools(self) -> None:
        assert frozenset({"close_page", "navigate_page", "new_page"}) == tool_registry.INSPECT_WARN_TOOLS

    def test_page_selecting_tools(self) -> None:
        assert frozenset({"new_page", "select_page"}) == tool_registry.PAGE_SELECTING_TOOLS

    def test_screenshot_gate_tools(self) -> None:
        assert frozenset({"take_screenshot"}) == tool_registry.SCREENSHOT_GATE_TOOLS

    def test_single_tab_tools(self) -> None:
        assert frozenset({"new_page"}) == tool_registry.SINGLE_TAB_TOOLS


class TestToolRegistryAll:
    """__all__ is a pinned literal; must go red if exported names change."""

    def test_all_exact(self) -> None:
        assert tool_registry.__all__ == [
            "CDP_TOOLS",
            "INSPECT_BLOCKED_TOOLS",
            "INSPECT_WARN_TOOLS",
            "INTERACTION_TOOLS",
            "NAVIGATION_TOOLS",
            "PAGE_SELECTING_TOOLS",
            "SCREENSHOT_GATE_TOOLS",
            "SINGLE_TAB_TOOLS",
            "TOOLS",
            "ToolFlags",
        ]


# ---------------------------------------------------------------------------
# Session layer
# ---------------------------------------------------------------------------


class TestSessionToolNames:
    """SESSION_TOOLS is a literal set of 6 names; must go red if one is added/removed."""

    def test_session_tools_exact(self) -> None:
        assert {
            "attach_browser",
            "list_profiles",
            "delete_profile",
            "use_browser_session",
            "browser_session_status",
            "close_browser",
        } == browser_session.SESSION_TOOLS
        assert len(browser_session.SESSION_TOOLS) == 6

    def test_session_tools_is_set(self) -> None:
        assert isinstance(browser_session.SESSION_TOOLS, set)


class TestCamoufoxExclusiveDispatch:
    """Four Camoufox-exclusive tool names are dispatched via inline branches.

    Must go red if a name is renamed or the error message for the
    missing-session case changes.
    """

    def _ctx(self, camoufox_ref: list[Any] | None = None) -> SessionDispatchContext:
        return SessionDispatchContext(
            controller_ref=[FakeController()],
            camoufox_ref=camoufox_ref if camoufox_ref is not None else [None],
            live_profile_conflict=[None],
        )

    def test_wait_for_human_without_session_errors(self) -> None:
        ctx = self._ctx([None])
        controller = FakeController()
        resp = dispatch_session_tool(ctx, controller, "wait_for_human", {})
        _assert_error_envelope(resp)
        text = "".join(extract_text_items(resp))
        assert "wait_for_human" in text
        assert "launch_camoufox" in text

    def test_get_cookies_without_session_errors(self) -> None:
        ctx = self._ctx([None])
        controller = FakeController()
        resp = dispatch_session_tool(ctx, controller, "get_cookies", {})
        _assert_error_envelope(resp)
        text = "".join(extract_text_items(resp))
        assert "get_cookies" in text
        assert "launch_camoufox" in text

    def test_wait_for_human_with_session_routes_to_camoufox(self) -> None:
        camoufox = FakeCamoufox({"result": {"resolved": True}})
        ctx = SessionDispatchContext(
            controller_ref=[FakeController()],
            camoufox_ref=[camoufox],
            live_profile_conflict=[None],
        )
        controller = FakeController()
        resp = dispatch_session_tool(ctx, controller, "wait_for_human", {"timeout": 5})
        # CamoufoxBackend wraps result via _wrap_result -> text_response
        _assert_bare_envelope(resp)
        assert camoufox.calls[0][0] == "wait_for_human"

    def test_get_cookies_with_session_routes_to_camoufox(self) -> None:
        camoufox = FakeCamoufox({"result": {"cookies": []}})
        ctx = SessionDispatchContext(
            controller_ref=[FakeController()],
            camoufox_ref=[camoufox],
            live_profile_conflict=[None],
        )
        controller = FakeController()
        resp = dispatch_session_tool(ctx, controller, "get_cookies", {})
        _assert_bare_envelope(resp)
        assert camoufox.calls[0][0] == "get_cookies"

    def test_launch_camoufox_dispatch_exists(self) -> None:
        # Verify the branch exists by inspecting source
        src = inspect.getsource(dispatch_session_tool)
        assert 'tool == "launch_camoufox"' in src

    def test_close_camoufox_dispatch_exists(self) -> None:
        src = inspect.getsource(dispatch_session_tool)
        assert 'tool == "close_camoufox"' in src

    def test_launch_camoufox_success_shape(self) -> None:
        fake_session = MagicMock()
        fake_session.call_tool.return_value = {"result": {"fingerprint": "abc"}}
        with patch("browser_tools.camoufox_session.CamoufoxSession", return_value=fake_session):
            resp = browser_session._handle_launch_camoufox([None], {})
        _assert_success_envelope(resp)
        assert "Camoufox" in "".join(extract_text_items(resp))

    def test_launch_camoufox_already_running_shape(self) -> None:
        resp = browser_session._handle_launch_camoufox([MagicMock()], {})
        _assert_success_envelope(resp)
        assert "already running" in "".join(extract_text_items(resp)).lower()

    def test_close_camoufox_no_session_shape(self) -> None:
        resp = browser_session._handle_close_camoufox([None])
        _assert_success_envelope(resp)

    def test_close_camoufox_with_session_shape(self) -> None:
        fake_session = MagicMock()
        fake_session.call_tool.return_value = {"status": "closed"}
        resp = browser_session._handle_close_camoufox([fake_session])
        _assert_success_envelope(resp)
        assert fake_session.call_tool.called


class TestCamoufoxToolMap:
    """CAMOUFOX_TOOL_MAP literal, including None for wait_for."""

    def test_exact(self) -> None:
        assert automation_backend.CAMOUFOX_TOOL_MAP == {
            "navigate_page": "navigate",
            "new_page": "navigate",
            "take_snapshot": "snapshot",
            "take_screenshot": "screenshot",
            "click": "click",
            "fill": "fill",
            "type_text": "fill",
            "evaluate_script": "evaluate",
            "wait_for": None,
        }

    def test_wait_for_explicitly_none(self) -> None:
        assert automation_backend.CAMOUFOX_TOOL_MAP["wait_for"] is None

    def test_length(self) -> None:
        assert len(automation_backend.CAMOUFOX_TOOL_MAP) == 9


class TestAutomationBackendProtocol:
    """AutomationBackend declares invoke(tool, args) and both adapters satisfy it."""

    def test_protocol_has_invoke(self) -> None:
        assert hasattr(AutomationBackend, "invoke")
        sig = inspect.signature(AutomationBackend.invoke)
        params = list(sig.parameters.keys())
        # Protocol invoke signature: (self, tool, args)
        assert params == ["self", "tool", "args"]

    def test_chrome_backend_invoke_signature(self) -> None:
        sig = inspect.signature(ChromeBackend.invoke)
        assert list(sig.parameters.keys()) == ["self", "tool", "args"]
        assert callable(getattr(ChromeBackend, "invoke", None))

    def test_camoufox_backend_invoke_signature(self) -> None:
        sig = inspect.signature(CamoufoxBackend.invoke)
        assert list(sig.parameters.keys()) == ["self", "tool", "args"]
        assert callable(getattr(CamoufoxBackend, "invoke", None))

    def test_both_expose_invoke(self) -> None:
        assert hasattr(ChromeBackend, "invoke")
        assert hasattr(CamoufoxBackend, "invoke")

    def test_chrome_backend_delegates(self) -> None:
        fake = FakeController()
        backend = ChromeBackend(fake)  # type: ignore[arg-type]
        backend.invoke("take_snapshot", {})
        assert fake.calls[0][0] == "take_snapshot"

    def test_camoufox_backend_maps_and_translates(self) -> None:
        camoufox = FakeCamoufox({"result": {"title": "t"}})
        backend = CamoufoxBackend(camoufox)  # type: ignore[arg-type]
        resp = backend.invoke("navigate_page", {"url": "https://x", "wait_until": "load"})
        assert camoufox.calls[0][0] == "navigate"
        _assert_bare_envelope(resp)

    def test_camoufox_backend_wait_for_unsupported(self) -> None:
        camoufox = FakeCamoufox()
        backend = CamoufoxBackend(camoufox)  # type: ignore[arg-type]
        resp = backend.invoke("wait_for", {})
        _assert_error_envelope(resp)
        assert "not supported" in "".join(extract_text_items(resp)).lower()


# ---------------------------------------------------------------------------
# Session handler arg shapes
# ---------------------------------------------------------------------------


class TestSessionHandlerArgShapes:
    """Each handler's consumed args keys via source introspection.

    Must go red if a parameter is renamed or removed.
    """

    def test_handle_attach_browser_keys(self) -> None:
        assert _args_get_keys(browser_session.handle_attach_browser) == {
            "endpoint",
            "tab_url",
            "profile",
            "mode",
            "stealth",
        }

    def test_handle_use_browser_session_keys(self) -> None:
        assert _args_get_keys(browser_session.handle_use_browser_session) == {
            "mode",
            "clear_active_attach",
            "profile",
            "endpoint",
            "browser_url",
            "channel",
            "viewport",
            "stealth",
            "headless",
            "isolated",
        }

    def test_handle_close_browser_keys(self) -> None:
        assert _args_get_keys(browser_session.handle_close_browser) == {"reset_session"}

    def test_handle_list_profiles_keys(self) -> None:
        assert _args_get_keys(browser_session.handle_list_profiles) == set()

    def test_handle_delete_profile_keys(self) -> None:
        assert _args_get_keys(browser_session.handle_delete_profile) == {"name"}

    def test_handle_browser_session_status_keys(self) -> None:
        assert _args_get_keys(browser_session.handle_browser_session_status) == set()

    def test_handle_launch_camoufox_keys(self) -> None:
        assert _args_get_keys(browser_session._handle_launch_camoufox) == set()

    def test_handle_close_camoufox_keys(self) -> None:
        assert _args_get_keys(browser_session._handle_close_camoufox) == set()

    # Smoke: each handler accepts a representative args dict without raising
    def test_handle_delete_profile_smoke(self) -> None:
        with patch("browser_tools.profile_catalog.delete_profile", return_value=True):
            resp = browser_session.handle_delete_profile({"name": "x"})
        _assert_bare_envelope(resp)

    def test_handle_list_profiles_smoke(self) -> None:
        with patch("browser_tools.profile_catalog.list_profiles", return_value=[]):
            resp = browser_session.handle_list_profiles({})
        _assert_bare_envelope(resp)

    def test_handle_browser_session_status_smoke(self) -> None:
        with patch(
            "browser_tools.browser_session.get_browser_session_status",
            return_value={"selected_source": "default_headless"},
        ):
            resp = browser_session.handle_browser_session_status({})
        _assert_bare_envelope(resp)

    def test_handle_close_browser_no_active(self) -> None:
        resp = browser_session.handle_close_browser([None], None, {})
        _assert_bare_envelope(resp)
        assert "No active browser" in "".join(extract_text_items(resp))

    def test_handle_attach_browser_with_endpoint_smoke(self) -> None:
        fake_state = MagicMock()
        fake_state.pid = None
        fake_state.browser_url = "http://127.0.0.1:9222"
        fake_controller = MagicMock()
        fake_controller.ensure_browser_state.return_value = fake_state
        fake_controller.state_path = MagicMock()
        with (
            patch("browser_tools.browser_session.validate_local_endpoint", return_value=None),
            patch(
                "browser_tools.browser_session.PersistentChromeController",
                return_value=fake_controller,
            ),
            patch("browser_tools.browser_session.save_active_attach_config"),
            patch(
                "browser_tools.persistent_browser.enumerate_tabs",
                return_value=[{"id": "1", "title": "T", "url": "https://x"}],
            ),
            patch("browser_tools.persistent_browser.select_tab_by_url", return_value=None),
        ):
            # minimal args with every key the handler reads
            resp = browser_session.handle_attach_browser(
                [None],
                {
                    "endpoint": "http://127.0.0.1:9222",
                    "tab_url": None,
                    "profile": None,
                    "mode": "full",
                    "stealth": False,
                },
            )
        _assert_bare_envelope(resp)

    def test_handle_use_browser_session_smoke(self) -> None:
        with (
            patch("browser_tools.browser_session.normalize_mode", return_value="headed-auth"),
            patch("browser_tools.browser_session.load_project_browser_config", return_value=None),
            patch("browser_tools.browser_session.save_session_override"),
            patch("browser_tools.browser_session.validate_local_endpoint", return_value=None),
        ):
            resp = browser_session.handle_use_browser_session(
                [None],
                {
                    "mode": "headed-auth",
                    "clear_active_attach": False,
                    "profile": "dev",
                    "endpoint": None,
                    "browser_url": None,
                    "channel": "canary",
                    "viewport": None,
                    "stealth": False,
                    "headless": False,
                    "isolated": False,
                },
            )
        _assert_bare_envelope(resp)


# ---------------------------------------------------------------------------
# Response envelope shape for session layer
# ---------------------------------------------------------------------------


class TestSessionResponseEnvelope:
    """Every session handler must return a bare envelope.

    Pin shape for both success and error paths where applicable.
    """

    def _check_envelope(self, resp: dict[str, Any], *, is_error: bool = False) -> None:
        if is_error:
            _assert_error_envelope(resp)
        else:
            _assert_success_envelope(resp)

    def test_handle_list_profiles_success(self) -> None:
        with patch("browser_tools.profile_catalog.list_profiles", return_value=[]):
            resp = browser_session.handle_list_profiles({})
        self._check_envelope(resp)

    def test_handle_list_profiles_with_profiles(self) -> None:
        with (
            patch("browser_tools.profile_catalog.list_profiles", return_value=["a"]),
            patch(
                "browser_tools.profile_catalog.describe_profile_runtime",
                return_value={
                    "profile": "a",
                    "pid": None,
                    "devtools_alive": False,
                    "endpoint": None,
                    "tab_count": 0,
                },
            ),
        ):
            resp = browser_session.handle_list_profiles({})
        self._check_envelope(resp)

    def test_handle_delete_profile_success(self) -> None:
        with patch("browser_tools.profile_catalog.delete_profile", return_value=True):
            resp = browser_session.handle_delete_profile({"name": "a"})
        self._check_envelope(resp)

    def test_handle_delete_profile_error_missing_name(self) -> None:
        with patch("browser_tools.profile_catalog.delete_profile", return_value=False):
            resp = browser_session.handle_delete_profile({"name": ""})
        self._check_envelope(resp, is_error=True)

    def test_handle_delete_profile_error_not_found(self) -> None:
        with patch("browser_tools.profile_catalog.delete_profile", return_value=False):
            resp = browser_session.handle_delete_profile({"name": "ghost"})
        self._check_envelope(resp, is_error=True)

    def test_handle_browser_session_status_success(self) -> None:
        with patch(
            "browser_tools.browser_session.get_browser_session_status",
            return_value={"selected_source": "default_headless"},
        ):
            resp = browser_session.handle_browser_session_status({})
        self._check_envelope(resp)

    def test_handle_close_browser_no_active(self) -> None:
        resp = browser_session.handle_close_browser([None], None, {})
        self._check_envelope(resp)
        # Built by hand, not via text_response, but shape must still match
        assert "result" in resp and "content" in resp["result"]

    def test_handle_close_browser_with_active(self) -> None:
        fake = MagicMock()
        with (
            patch(
                "browser_tools.persistent_browser.close_active_session",
                return_value={"quit_chrome": True, "pid": 123, "detached": False, "endpoint": None},
            ),
        ):
            resp = browser_session.handle_close_browser([fake], None, {})
        self._check_envelope(resp)

    def test_handle_close_browser_with_reset_session(self) -> None:
        fake = MagicMock()
        with (
            patch(
                "browser_tools.persistent_browser.close_active_session",
                return_value={"quit_chrome": False, "detached": True, "endpoint": "http://x", "pid": 1},
            ),
            patch("browser_tools.session_store.clear_session_override"),
        ):
            resp = browser_session.handle_close_browser([fake], None, {"reset_session": True})
        self._check_envelope(resp)
        assert "Session override cleared" in "".join(extract_text_items(resp))

    def test_handle_use_browser_session_clear_mode(self) -> None:
        with (
            patch("browser_tools.browser_session.clear_session_override"),
            patch("browser_tools.browser_session.clear_active_attach_config"),
        ):
            resp = browser_session.handle_use_browser_session([None], {"mode": "clear"})
        self._check_envelope(resp)

    def test_launch_camoufox_success(self) -> None:
        fake = MagicMock()
        fake.call_tool.return_value = {"result": {"fingerprint": "fp"}}
        with patch("browser_tools.camoufox_session.CamoufoxSession", return_value=fake):
            resp = browser_session._handle_launch_camoufox([None], {})
        self._check_envelope(resp)

    def test_close_camoufox_success(self) -> None:
        fake = MagicMock()
        resp = browser_session._handle_close_camoufox([fake])
        self._check_envelope(resp)


# ---------------------------------------------------------------------------
# mcp_response builders
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CDP handler table
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Daemon dispatch routing
# ---------------------------------------------------------------------------


class TestCdpDispatchRouting:
    """Pin dispatch_tool routing for CDP, screenshot gate, default, inspect, nav."""

    def test_cdp_tool_routes_to_cdp_handler(self) -> None:
        cdp = FakeCdpHandler()
        broker = FakeBroker()
        ctx = DispatchContext(broker, cdp)  # type: ignore[arg-type]
        resp = dispatch_tool(_request("list_frames", {"depth": 2}), 3, ctx)
        assert cdp.calls == [("list_frames", {"depth": 2})]
        assert resp["id"] == 3
        assert "jsonrpc" in resp
        assert broker.requests == []

    def test_screenshot_gate_not_routed_to_cdp(self) -> None:
        canned = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "shot"}]}}
        cdp = FakeCdpHandler()
        broker = FakeBroker()
        ctx = DispatchContext(broker, cdp)  # type: ignore[arg-type]
        with patch("browser_tools.mcp_daemon._take_screenshot_with_paint_gate", return_value=canned) as gate:
            req = _request("take_screenshot")
            resp = dispatch_tool(req, 9, ctx)
        gate.assert_called_once_with(req, 9, broker, cdp)
        assert cdp.calls == []
        assert broker.requests == []
        assert resp == canned

    def test_default_forwarded_tool_passes_through_unchanged(self) -> None:
        broker = FakeBroker()
        cdp = FakeCdpHandler()
        ctx = DispatchContext(broker, cdp)  # type: ignore[arg-type]
        # take_snapshot is the canonical default-forwarded tool not in TOOLS CDP/screenshot sets
        req = _request("take_snapshot", {"compact": True})
        resp = dispatch_tool(req, 2, ctx)
        assert len(broker.requests) == 1
        assert broker.requests[0]["method"] == "tools/call"
        assert broker.requests[0]["params"] == req["params"]
        assert resp["id"] == 2
        assert cdp.calls == []

    def test_default_forwarded_arbitrary_tool(self) -> None:
        broker = FakeBroker()
        ctx = DispatchContext(broker, FakeCdpHandler())  # type: ignore[arg-type]
        req = _request("list_pages", {})
        dispatch_tool(req, 1, ctx)
        assert broker.requests[0]["params"]["name"] == "list_pages"

    def test_inspect_mode_blocks_inspect_blocked_tool(self) -> None:
        broker = FakeBroker()
        cdp = FakeCdpHandler(mode="inspect")
        ctx = DispatchContext(broker, cdp)  # type: ignore[arg-type]
        resp = dispatch_tool(_request("click"), 7, ctx)
        assert resp["id"] == 7
        text = "".join(extract_text_items(resp))
        assert "E004" in text
        assert "click" in text
        assert "blocked in inspect mode" in text
        assert broker.requests == []
        assert cdp.calls == []

    def test_inspect_mode_error_mentions_observation_tools(self) -> None:
        cdp = FakeCdpHandler(mode="inspect")
        ctx = DispatchContext(FakeBroker(), cdp)  # type: ignore[arg-type]
        resp = dispatch_tool(_request("press_key"), 1, ctx)
        text = "".join(extract_text_items(resp))
        assert "take_snapshot" in text
        assert "take_screenshot" in text
        assert "list_pages" in text

    def test_inspect_mode_allows_unblocked_tool(self) -> None:
        broker = FakeBroker()
        ctx = DispatchContext(broker, FakeCdpHandler(mode="inspect"))  # type: ignore[arg-type]
        dispatch_tool(_request("take_snapshot"), 1, ctx)
        assert len(broker.requests) == 1

    def test_navigation_triggers_interstitial(self) -> None:
        cdp = FakeCdpHandler(
            detection={"detections": [{"type": "cloudflare"}], "auto_retried": False, "retries_used": 0}
        )
        ctx = DispatchContext(FakeBroker(), cdp)  # type: ignore[arg-type]
        with patch("browser_tools.mcp_daemon.format_interstitials", return_value="CHALLENGE"):
            resp = dispatch_tool(_request("navigate_page", {"url": "https://x"}), 1, ctx)
        assert cdp.detection_runs == 1
        assert "CHALLENGE" in "".join(extract_text_items(resp))

    def test_non_navigation_skips_interstitial(self) -> None:
        cdp = FakeCdpHandler(detection={"detections": [{"type": "cloudflare"}]})
        ctx = DispatchContext(FakeBroker(), cdp)  # type: ignore[arg-type]
        dispatch_tool(_request("take_snapshot"), 1, ctx)
        assert cdp.detection_runs == 0

    def test_navigation_skips_interstitial_on_error(self) -> None:
        broker = FakeBroker(response={"jsonrpc": "2.0", "error": {"code": -1, "message": "boom"}})
        cdp = FakeCdpHandler(detection={"detections": [{"type": "cloudflare"}]})
        ctx = DispatchContext(broker, cdp)  # type: ignore[arg-type]
        dispatch_tool(_request("navigate_page"), 1, ctx)
        assert cdp.detection_runs == 0


# ---------------------------------------------------------------------------
# Default-forwarded literal freeze (best-effort)
# ---------------------------------------------------------------------------


class TestDefaultForwardedLiterals:
    """Freeze the names of default-forwarded chrome-devtools-mcp tools.

    These have no Python-owned schema; the only contract is their names
    appear as literals in Python source. This test scans the source for
    those literals so a rename/removal would be noticed. It is bounded by
    what the Python source mentions, not a claim to enumerate every
    chrome-devtools-mcp tool (that schema lives in the Node package).
    """

    # Names that appear in mcp_daemon's E004 inspect-mode message
    E004_LITERALS = frozenset(
        {
            "take_snapshot",
            "take_screenshot",
            "list_pages",
            "evaluate_script",
            "list_console_messages",
            "list_network_requests",
            "list_frames",
            "get_frame_storage",
        }
    )

    def test_e004_message_contains_expected_tools(self) -> None:
        src = inspect.getsource(mcp_daemon.dispatch_tool)
        for name in self.E004_LITERALS:
            assert name in src, f"E004 message missing {name!r}"

    def test_true_default_forwarded_subset(self) -> None:
        # Only those absent from tool_registry.TOOLS are true default-forwarded
        true_default = self.E004_LITERALS - set(tool_registry.TOOLS)
        assert true_default == frozenset(
            {
                "take_snapshot",
                "list_pages",
                "evaluate_script",
                "list_console_messages",
                "list_network_requests",
            }
        )

    def test_camoufox_map_mentions_default_forwarded(self) -> None:
        # take_snapshot / evaluate_script are default-forwarded names reused via CAMOUFOX_TOOL_MAP
        assert "take_snapshot" in automation_backend.CAMOUFOX_TOOL_MAP
        assert "evaluate_script" in automation_backend.CAMOUFOX_TOOL_MAP
        assert "wait_for" in automation_backend.CAMOUFOX_TOOL_MAP

    def test_source_mentions_core_default_tools(self) -> None:
        # Best-effort scan: at least these literals appear somewhere in src/
        import pathlib

        src_root = pathlib.Path(__file__).resolve().parents[1] / "src" / "browser_tools"
        all_text = ""
        for p in src_root.rglob("*.py"):
            all_text += p.read_text(encoding="utf-8", errors="ignore")
        for name in ["take_snapshot", "list_pages", "evaluate_script", "wait_for"]:
            assert f'"{name}"' in all_text or f"'{name}'" in all_text, f"literal {name!r} not found in src"
