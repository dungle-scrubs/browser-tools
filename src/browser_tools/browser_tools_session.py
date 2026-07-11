#!/usr/bin/env python3
"""
Chrome DevTools MCP Wrapper
Simplified CLI for Chrome DevTools MCP with snapshot-based architecture
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .browser_state import (
    AUTH_MODES,
    HEADED_AUTH_MODES,
    HEADLESS_AUTH_MODES,
    normalize_mode,
)
from .chrome_config import get_mcp_command
from .chrome_utils import (
    BrowserToolsError,
    error_exit,
    format_response,
    invoke_mcp_tool,
    success_output,
)
from .persistent_browser import (
    PersistentChromeController,
    ProjectBrowserConfig,
    clear_active_attach_config,
    clear_session_override,
    create_project_preferred_controller,
    create_session_override_controller,
    find_live_profiles,
    get_browser_session_status,
    load_active_attach_controller,
    load_project_browser_config,
    save_active_attach_config,
    save_session_override,
)
from .process_utils import validate_local_endpoint


def _tool_error(text: str) -> dict[str, Any]:
    """Build a JSON-RPC error response with a single text block.

    Args:
        text: Human-readable error message.

    Returns:
        JSON-RPC response dict flagged as an error.
    """
    return {"result": {"content": [{"type": "text", "text": text}], "isError": True}}


def create_parser():
    """Create argument parser"""
    parser = argparse.ArgumentParser(
        description="Chrome DevTools MCP Wrapper - Snapshot-based automation",
        epilog="For element interaction, first use take-snapshot to get element UIDs",
    )

    # Global options
    parser.add_argument("--headless", action="store_true", help="Run Chrome without UI")
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Use a dedicated profile directory separate from named profiles "
        "(login state is not shared with the default or named-profile sessions)",
    )
    parser.add_argument("--viewport", type=str, help="Initial viewport (e.g., 1280x720)")
    parser.add_argument(
        "--channel",
        type=str,
        default="canary",
        choices=["stable", "canary", "beta", "dev"],
        help="Chrome channel (default: canary)",
    )
    parser.add_argument(
        "--browser-url", type=str, help="Connect to existing Chrome (e.g., http://localhost:9222)"
    )
    parser.add_argument(
        "--format",
        type=str,
        default="text",
        choices=["text", "json", "pretty"],
        help="Output format",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # === High-Level Helper ===
    screenshot_url_parser = subparsers.add_parser(
        "screenshot-url", help="Capture screenshot of URL"
    )
    screenshot_url_parser.add_argument("--url", required=True, help="URL to screenshot")
    screenshot_url_parser.add_argument("--file-path", required=True, help="Path to save screenshot")
    screenshot_url_parser.add_argument("--full-page", action="store_true", help="Capture full page")
    screenshot_url_parser.add_argument(
        "--format-type", choices=["png", "jpeg", "webp"], help="Image format"
    )

    # === Direct Tool Access ===

    # new_page - Create new page
    new_page_parser = subparsers.add_parser("new-page", help="Create a new page")
    new_page_parser.add_argument("--url", required=True, help="URL to load")

    # navigate_page - Navigate
    nav_parser = subparsers.add_parser("navigate", help="Navigate page")
    nav_parser.add_argument("--type", default="url", choices=["url", "back", "forward", "reload"])
    nav_parser.add_argument("--url", help="URL to navigate to (required for type=url)")

    # list_pages - List pages
    subparsers.add_parser("list-pages", help="List all open pages")

    # select_page - Select page
    select_parser = subparsers.add_parser("select-page", help="Select a page")
    select_parser.add_argument(
        "--page-id",
        "--page-idx",
        dest="page_id",
        type=int,
        required=True,
        help="Page ID from list-pages output",
    )

    # take_snapshot - Take snapshot
    snapshot_parser = subparsers.add_parser("take-snapshot", help="Take accessibility snapshot")
    snapshot_parser.add_argument("--verbose", action="store_true", help="Include full a11y tree")
    snapshot_parser.add_argument("--file-path", help="Save to file")

    # take_screenshot - Take screenshot
    screenshot_parser = subparsers.add_parser("take-screenshot", help="Take screenshot")
    screenshot_parser.add_argument("--uid", help="Element UID to screenshot")
    screenshot_parser.add_argument("--full-page", action="store_true", help="Full page screenshot")
    screenshot_parser.add_argument("--file-path", help="Save to file")
    screenshot_parser.add_argument(
        "--format-type", choices=["png", "jpeg", "webp"], help="Image format"
    )
    screenshot_parser.add_argument("--quality", type=int, help="JPEG/WebP quality (0-100)")

    # wait_for - Wait for text
    wait_parser = subparsers.add_parser("wait-for", help="Wait for text to appear")
    wait_parser.add_argument("--text", required=True, help="Text to wait for")

    # click - Click element
    click_parser = subparsers.add_parser("click", help="Click element by UID")
    click_parser.add_argument("--uid", required=True, help="Element UID from snapshot")
    click_parser.add_argument("--dbl-click", action="store_true", help="Double click")

    # fill - Fill input
    fill_parser = subparsers.add_parser("fill", help="Fill input by UID")
    fill_parser.add_argument("--uid", required=True, help="Element UID from snapshot")
    fill_parser.add_argument("--value", required=True, help="Value to fill")

    # evaluate_script - Execute JS
    eval_parser = subparsers.add_parser("evaluate", help="Execute JavaScript function")
    eval_parser.add_argument("--function", required=True, help="JavaScript function")

    # press_key - Press key
    key_parser = subparsers.add_parser("press-key", help="Press keyboard key")
    key_parser.add_argument(
        "--key", required=True, help="Key or combination (e.g., Enter, Control+A)"
    )

    # resize_page - Resize viewport
    resize_parser = subparsers.add_parser("resize", help="Resize viewport")
    resize_parser.add_argument("--width", type=int, required=True, help="Width in pixels")
    resize_parser.add_argument("--height", type=int, required=True, help="Height in pixels")

    # list_console_messages - List console
    console_list_parser = subparsers.add_parser("console-list", help="List console messages")
    console_list_parser.add_argument("--page-size", type=int, help="Max messages to return")

    # list_network_requests - List network
    network_list_parser = subparsers.add_parser("network-list", help="List network requests")
    network_list_parser.add_argument("--page-size", type=int, help="Max requests to return")

    # performance_start_trace - Start trace
    perf_start_parser = subparsers.add_parser("perf-start", help="Start performance trace")
    perf_start_parser.add_argument(
        "--reload", action="store_true", help="Reload page after starting"
    )

    # performance_stop_trace - Stop trace
    subparsers.add_parser("perf-stop", help="Stop performance trace")

    # profiler_timed - CPU profile for fixed duration
    profiler_timed = subparsers.add_parser("profiler-timed", help="CPU profile for N seconds")
    profiler_timed.add_argument("--duration", type=float, default=5.0, help="Duration in seconds")
    profiler_timed.add_argument("--port", type=int, default=9222, help="Chrome debug port")

    # profiler_watch - Wait for CPU spike then capture
    profiler_watch = subparsers.add_parser("profiler-watch", help="Profile on CPU spike")
    profiler_watch.add_argument("--threshold", type=float, default=80.0, help="CPU threshold %")
    profiler_watch.add_argument("--timeout", type=float, default=60.0, help="Max wait seconds")
    profiler_watch.add_argument("--window", type=float, default=3.0, help="Capture window seconds")
    profiler_watch.add_argument("--port", type=int, default=9222, help="Chrome debug port")

    return parser


def create_persistent_controller(
    args: argparse.Namespace, force_persistent: bool = False
) -> PersistentChromeController:
    """Create a persistent browser controller for multi-step flows.

    Args:
        args: Parsed CLI arguments namespace.
        force_persistent: Force persistent browser reuse even without CLI flags.

    Returns:
        Configured PersistentChromeController instance.
    """
    return PersistentChromeController(
        headless=args.headless,
        isolated=args.isolated,
        viewport=args.viewport,
        channel=args.channel,
        browser_url=getattr(args, "browser_url", None),
        force_persistent=force_persistent,
    )


def execute_screenshot_url(args: argparse.Namespace) -> int:
    """High-level helper: Screenshot a URL"""
    try:
        controller = create_persistent_controller(args, force_persistent=True)

        # Step 1: Create new page with URL
        controller.invoke_tool("new_page", {"url": args.url})

        # Step 2: Wait for page to load (wait for common text)
        import contextlib

        with contextlib.suppress(BrowserToolsError):
            controller.invoke_tool("wait_for", {"text": "html"})

        # Step 3: Take screenshot
        screenshot_params = {"filePath": args.file_path}
        if args.full_page:
            screenshot_params["fullPage"] = True
        if args.format_type:
            screenshot_params["format"] = args.format_type

        controller.invoke_tool("take_screenshot", screenshot_params)

        success_output(f"Screenshot saved to {args.file_path}", format_type=args.format)
        return 0

    except BrowserToolsError as e:
        error_exit(str(e))


def execute_direct_tool(tool_name: str, params: dict[str, Any], args: argparse.Namespace) -> int:
    """Execute a direct MCP tool"""
    try:
        controller = create_persistent_controller(args)
        if controller.should_use_persistent_browser():
            response = controller.invoke_tool(tool_name, params)
        else:
            config = get_mcp_command(
                headless=args.headless,
                isolated=args.isolated,
                viewport=args.viewport,
                channel=args.channel,
                browser_url=getattr(args, "browser_url", None),
            )
            response = invoke_mcp_tool(tool_name, params, config)
        output = format_response(response, args.format)
        print(output)
        return 0

    except BrowserToolsError as e:
        error_exit(str(e))


def execute_profiler_timed(args: argparse.Namespace) -> int:
    """Execute timed CPU profiler via local script"""
    import asyncio

    from .profiler import profile_page

    try:
        result = asyncio.run(
            profile_page(duration=args.duration, port=args.port, format_type=args.format)
        )
        print(result)
        return 0
    except (RuntimeError, OSError) as e:
        error_exit(str(e))


def execute_profiler_watch(args: argparse.Namespace) -> int:
    """Execute CPU profiler that watches for high CPU"""
    import asyncio

    from .profiler import profile_until_high_cpu

    try:
        result = asyncio.run(
            profile_until_high_cpu(
                threshold=args.threshold,
                timeout=args.timeout,
                sample_window=args.window,
                port=args.port,
                format_type=args.format,
            )
        )
        print(result)
        return 0
    except (RuntimeError, OSError) as e:
        error_exit(str(e))


def handle_attach_browser(controller_ref: list[Any], args: Any) -> dict[str, Any]:
    """Handle the attach_browser tool by creating a new controller.

    Endpoint is optional when ``profile`` is provided: this looks up the
    Chrome process holding that profile via its singleton lock and uses its
    actual remote-debugging port. When both are given, the connected Chrome
    is checked against the profile's user-data-dir to catch port collisions
    and silent attach-to-the-wrong-Chrome failures.

    Args:
        controller_ref: Single-element list holding the active controller.
        args: Tool arguments with endpoint, tab_url, profile, mode.

    Returns:
        JSON-RPC response dict.
    """
    from .persistent_browser import (
        CACHE_DIR,
        enumerate_tabs,
        find_chrome_debug_port,
        find_chrome_user_data_dir,
        is_devtools_available,
        is_process_alive,
        read_singleton_lock_pid,
        select_tab_by_url,
    )

    endpoint = (args.get("endpoint") or "").strip() or None
    if endpoint is not None:
        endpoint_error = validate_local_endpoint(endpoint)
        if endpoint_error:
            return _tool_error(f"Error: {endpoint_error}")
    tab_url = args.get("tab_url")
    profile = args.get("profile")
    mode = args.get("mode", "full")
    stealth = args.get("stealth", False)

    discovered_pid: int | None = None
    if endpoint is None:
        if not profile:
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Error: pass either 'endpoint' (e.g. http://127.0.0.1:9222) or "
                                "'profile' (a named profile from list_profiles). With profile alone, "
                                "browser-tools discovers the running Chrome's debug port automatically."
                            ),
                        }
                    ],
                    "isError": True,
                }
            }
        profile_dir = CACHE_DIR / "profiles" / profile
        if not profile_dir.exists():
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: profile '{profile}' not found at {profile_dir}. "
                                "Call list_profiles to see available profiles."
                            ),
                        }
                    ],
                    "isError": True,
                }
            }
        lock_pid = read_singleton_lock_pid(profile_dir)
        if lock_pid is None or not is_process_alive(lock_pid):
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: profile '{profile}' has no running Chrome. "
                                f"Either launch Chrome on this profile and retry, or call "
                                f"use_browser_session(mode='headed-auth', profile='{profile}') to launch one."
                            ),
                        }
                    ],
                    "isError": True,
                }
            }
        port = find_chrome_debug_port(lock_pid)
        if port is None:
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: Chrome (pid {lock_pid}) holding profile '{profile}' has no "
                                "--remote-debugging-port flag. Restart it with that flag, or kill it "
                                f"and call use_browser_session(mode='headed-auth', profile='{profile}')."
                            ),
                        }
                    ],
                    "isError": True,
                }
            }
        candidate = f"http://127.0.0.1:{port}"
        if not is_devtools_available(candidate):
            from .persistent_browser import format_dead_port_error

            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": format_dead_port_error(profile, profile_dir, lock_pid),
                        }
                    ],
                    "isError": True,
                }
            }
        endpoint = candidate
        discovered_pid = lock_pid

    # Validate profile/endpoint coherence: the Chrome at endpoint should be
    # running the requested profile's user-data-dir. Catches the case where
    # an unrelated Chrome stole the port we expected.
    if profile is not None:
        expected_dir = (CACHE_DIR / "profiles" / profile).resolve()
        listeners = (
            find_listeners_on_endpoint(endpoint) if discovered_pid is None else [discovered_pid]
        )
        actual_dir = None
        actual_pid = None
        for pid in listeners:
            actual_dir = find_chrome_user_data_dir(pid)
            actual_pid = pid
            if actual_dir is not None:
                break
        if actual_dir is not None and actual_dir != expected_dir:
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: the Chrome at {endpoint} is running user-data-dir "
                                f"{actual_dir} (pid {actual_pid}), not the requested profile "
                                f"'{profile}' ({expected_dir}). Drop the explicit endpoint and call "
                                f"attach_browser(profile='{profile}') to auto-discover the right port."
                            ),
                        }
                    ],
                    "isError": True,
                }
            }

    # Create a new controller configured for the external browser
    new_controller = PersistentChromeController(
        isolated=False,
        browser_url=endpoint,
        profile=profile,
        stealth=stealth,
        force_persistent=True,
    )
    new_controller.mode = mode
    controller_ref[0] = new_controller

    state = new_controller.ensure_browser_state()
    save_active_attach_config(
        endpoint,
        profile=profile,
        mode=mode,
        stealth=stealth,
    )

    # Enumerate tabs
    tabs = enumerate_tabs(endpoint)
    if not tabs:
        return {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: E001 - Could not connect to Chrome at {endpoint}. Launch Chrome with --remote-debugging-port=9222",
                    }
                ],
                "isError": True,
            }
        }

    # Auto-select tab if pattern provided
    selected_tab = None
    if tab_url:
        selected_tab = select_tab_by_url(tabs, tab_url)
        if selected_tab is not None:
            state.selected_page_id = None
            state.selected_page_url = selected_tab.get("url")
            state.save(new_controller.state_path)

    lines = [f"Connected to Chrome at {endpoint}"]
    if discovered_pid is not None:
        lines[0] += f" (auto-discovered from profile '{profile}', pid {discovered_pid})"
    if mode == "inspect":
        lines.append("Mode: INSPECT (read-only, interaction tools blocked)")
    if stealth:
        lines.append("Stealth: ON (automation fingerprinting patches active)")
    lines.append(f"\nOpen tabs ({len(tabs)}):")
    for i, tab in enumerate(tabs):
        marker = " [auto-selected]" if selected_tab and tab["id"] == selected_tab["id"] else ""
        lines.append(f"  {i}: {tab.get('title', 'Untitled')} - {tab.get('url', '')}{marker}")

    if profile and discovered_pid is None:
        lines.append(f"\nProfile: {profile}")

    return {"result": {"content": [{"type": "text", "text": "\n".join(lines)}]}}


def find_listeners_on_endpoint(endpoint: str) -> list[int]:
    """Return PIDs listening on the TCP port encoded in ``endpoint``.

    Args:
        endpoint: Chrome debug endpoint, e.g. ``http://127.0.0.1:9222``.

    Returns:
        Distinct PIDs, or empty when the port cannot be parsed or lsof is
        unavailable.
    """
    from urllib.parse import urlparse

    from .persistent_browser import find_listeners_on_port

    try:
        port = urlparse(endpoint).port
    except ValueError:
        return []
    if port is None:
        return []
    return find_listeners_on_port(int(port))


def handle_list_profiles(args: Any) -> dict[str, Any]:
    """Handle the list_profiles tool.

    Reports each profile's runtime state (live PID, debug port, current URL,
    port-collision details) so the agent can pick a profile to attach to
    without guessing endpoints.

    Args:
        args: Tool arguments (none required).

    Returns:
        JSON-RPC response dict.
    """
    del args
    from .persistent_browser import describe_profile_runtime, list_profiles

    profiles = list_profiles()
    if not profiles:
        return {"result": {"content": [{"type": "text", "text": "No named profiles found."}]}}

    statuses = [describe_profile_runtime(name) for name in profiles]
    lines = ["Named profiles:"]
    for status in statuses:
        name = status["profile"]
        if status["devtools_alive"]:
            url = status.get("current_url") or "<no open tab>"
            lines.append(
                f"  {name} — live (pid {status['pid']}, {status['endpoint']}, "
                f"{status['tab_count']} tabs) — {url}"
            )
        elif status["pid"] is not None:
            intended = status.get("intended_port")
            collisions = status.get("port_collision_pids") or []
            detail = (
                f"port {intended} held by pid(s) {', '.join(str(p) for p in collisions)}"
                if collisions
                else "debug port unreachable"
            )
            lines.append(
                f"  {name} — process pid {status['pid']} alive but {detail}. "
                f"Use attach_browser(profile='{name}') for a clear error."
            )
        else:
            lines.append(
                f"  {name} — not running. "
                f"use_browser_session(mode='headed-auth', profile='{name}') will launch it."
            )

    payload = {"profiles": statuses, "summary": "\n".join(lines)}
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]
        }
    }


def handle_delete_profile(args: Any) -> dict[str, Any]:
    """Handle the delete_profile tool.

    Args:
        args: Tool arguments with 'name'.

    Returns:
        JSON-RPC response dict.
    """
    from .persistent_browser import delete_profile

    name = args.get("name", "")
    if not name:
        return {
            "result": {
                "content": [{"type": "text", "text": "Error: profile name is required"}],
                "isError": True,
            }
        }

    if delete_profile(name):
        return {"result": {"content": [{"type": "text", "text": f"Profile '{name}' deleted."}]}}
    return {
        "result": {
            "content": [{"type": "text", "text": f"Error: Profile '{name}' not found."}],
            "isError": True,
        }
    }


def handle_browser_session_status(args: Any) -> dict[str, Any]:
    """Handle the browser_session_status tool.

    Args:
        args: Tool arguments (none required).

    Returns:
        JSON-RPC response dict with browser session diagnostics.
    """
    del args
    text = json.dumps(get_browser_session_status(), indent=2, sort_keys=True)
    return {"result": {"content": [{"type": "text", "text": text}]}}


def handle_use_browser_session(controller_ref: list[Any], args: Any) -> dict[str, Any]:
    """Handle the use_browser_session tool.

    Args:
        controller_ref: Single-element list holding the active controller.
        args: Desired browser session mode and options.

    Returns:
        JSON-RPC response dict.
    """
    mode = normalize_mode(args.get("mode", "project"))
    if mode in {"clear", "default", "project"}:
        clear_session_override()
        if args.get("clear_active_attach", False):
            clear_active_attach_config()
        controller_ref[0] = None
        return {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Browser session override cleared; project preference will be used.",
                    }
                ]
            }
        }

    project_config = load_project_browser_config()
    profile = args.get("profile")
    endpoint = args.get("endpoint") or args.get("browser_url")
    channel = args.get("channel") or (project_config.channel if project_config else "canary")
    viewport = args.get("viewport") or (project_config.viewport if project_config else None)
    stealth = bool(args.get("stealth", project_config.stealth if project_config else False))

    if mode in AUTH_MODES:
        if profile is None and project_config is not None:
            profile = project_config.profile
        if endpoint is None and project_config is not None and mode not in HEADLESS_AUTH_MODES:
            endpoint = project_config.endpoint or project_config.browser_url
        if profile is None and endpoint is None:
            profile = "google-auth"

    # Validate the resolved endpoint (whether from args or the project config)
    # before it is persisted and later dialed.
    if endpoint is not None:
        endpoint_error = validate_local_endpoint(endpoint)
        if endpoint_error:
            return _tool_error(f"Error: {endpoint_error}")

    config = ProjectBrowserConfig(
        mode=mode,
        profile=profile,
        endpoint=endpoint,
        headless=args.get("headless"),
        isolated=args.get("isolated"),
        channel=channel,
        viewport=viewport,
        stealth=stealth,
    )
    save_session_override(config)
    controller_ref[0] = None

    lines = [f"Browser session override set: {mode}"]
    if profile:
        lines.append(f"Profile: {profile}")
    if endpoint:
        lines.append(f"Endpoint: {endpoint}")
    if mode in HEADLESS_AUTH_MODES:
        lines.append(
            "Headless auth uses a persistent profile, but Google may challenge or invalidate automated headless sessions."
        )
    if mode in HEADED_AUTH_MODES and endpoint is None:
        lines.append("A headed Chrome session will be launched/reused with the configured profile.")
    return {"result": {"content": [{"type": "text", "text": "\n".join(lines)}]}}


# Session-level tools that don't need MCP daemon
SESSION_TOOLS = {
    "attach_browser",
    "list_profiles",
    "delete_profile",
    "use_browser_session",
    "browser_session_status",
}

# Tools routed through Camoufox when active. Maps browser-tools names to
# camoufox session tool names. Keys present here will be intercepted; if
# the mapped value is None the original tool name is forwarded as-is.
_CAMOUFOX_TOOL_MAP: dict[str, str | None] = {
    "navigate_page": "navigate",
    "new_page": "navigate",  # camoufox uses single page, navigate covers this
    "take_snapshot": "snapshot",
    "take_screenshot": "screenshot",
    "click": "click",
    "fill": "fill",
    "type_text": "fill",  # alias
    "evaluate_script": "evaluate",
    "wait_for": None,  # not mapped — use wait_for_human instead
}

# Camoufox-exclusive tools handled directly by the camoufox session
_CAMOUFOX_ONLY_TOOLS = {"launch_camoufox", "wait_for_human", "get_cookies", "close_camoufox"}


def _translate_args_for_camoufox(chrome_tool: str, args: Any) -> dict[str, Any]:
    """Translate browser-tools arg names to camoufox arg names.

    Args:
        chrome_tool: Original browser-tools tool name.
        args: Original arguments.

    Returns:
        Args dict adapted for the camoufox session.
    """
    if chrome_tool in ("navigate_page", "new_page"):
        return {"url": args.get("url", ""), "wait_until": args.get("wait_until", "load")}
    if chrome_tool == "take_screenshot":
        return {
            "path": args.get("filePath", args.get("path", "")),
            "full_page": args.get("fullPage", False),
        }
    if chrome_tool == "click":
        uid = args.get("uid", "")
        return {"selector": uid}
    if chrome_tool in ("fill", "type_text"):
        return {"selector": args.get("uid", ""), "value": args.get("value", "")}
    if chrome_tool == "evaluate_script":
        return {"script": args.get("function", "")}
    return args


def _camoufox_result_to_mcp(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a camoufox session result in MCP JSON-RPC format.

    Args:
        result: CamoufoxSession.call_tool() return value.

    Returns:
        MCP-style response with content array.
    """
    import json as _json

    if "error" in result:
        return {
            "result": {
                "content": [{"type": "text", "text": f"Error: {result['error']}"}],
                "isError": True,
            }
        }
    text = _json.dumps(result.get("result", result), indent=2)
    return {"result": {"content": [{"type": "text", "text": text}]}}


def choose_live_profile_fallback() -> tuple[
    PersistentChromeController | None, list[dict[str, Any]]
]:
    """Decide whether to auto-attach to a live profile when no session is configured.

    Returns:
        ``(controller, live)``.

        - ``controller`` is non-None only when exactly one named profile has a
          reachable DevTools endpoint; in that case it is configured to reuse
          that running Chrome.
        - ``live`` is the full list of live-profile descriptors. When it has
          more than one entry the caller should refuse to launch a new browser
          and instead surface the list so the agent can pick one explicitly.
    """
    live = find_live_profiles()
    if len(live) == 1:
        info = live[0]
        controller = PersistentChromeController(
            headless=False,
            isolated=False,
            channel="canary",
            profile=info["profile"],
            force_persistent=True,
        )
        controller.mode = "full"
        return controller, live
    return None, live


def select_default_controller() -> tuple[PersistentChromeController, list[dict[str, Any]] | None]:
    """Pick a controller when no explicit session is configured.

    Prefers a sole live profile over a fresh headless-isolated Chrome so the
    agent does not start "random new sessions" while a real browser is open.

    Returns:
        ``(controller, conflict)``.

        - When exactly one profile is live, returns its reuse controller and
          ``conflict=None``.
        - When zero profiles are live, returns the default headless-isolated
          controller and ``conflict=None``.
        - When multiple profiles are live, returns the default
          headless-isolated controller and the live-profile list as
          ``conflict`` so the caller can refuse non-session tools.
    """
    fallback, live = choose_live_profile_fallback()
    if fallback is not None:
        return fallback, None
    default = PersistentChromeController(
        headless=True,
        isolated=True,
        channel="canary",
        force_persistent=True,
    )
    conflict = live if len(live) > 1 else None
    return default, conflict


def _format_live_profile_conflict_error(live: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a tool-proxy error response describing multiple live profiles.

    Args:
        live: ``describe_profile_runtime`` descriptors for live profiles.

    Returns:
        MCP-style error response telling the agent how to disambiguate.
    """
    lines = [
        f"Multiple browser profiles are live ({len(live)}). Refusing to launch a new headless",
        "Chrome that would ignore them. Pick one explicitly:",
        "",
    ]
    for info in live:
        name = info.get("profile", "?")
        endpoint = info.get("endpoint") or "?"
        url = info.get("current_url") or "(no active page)"
        tabs = info.get("tab_count", 0)
        lines.append(f"  - {name}: {endpoint} ({tabs} tab(s)) — {url}")
    lines.extend(
        [
            "",
            "Recover with one of:",
            "  use_browser_session(mode='headed-auth', profile='<name>')",
            "  attach_browser(profile='<name>')",
            "Or call use_browser_session(mode='headless') to opt into a fresh isolated session.",
        ]
    )
    return {
        "result": {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "isError": True,
        }
    }


def create_tool_proxy_handlers():
    """Create stateful handlers for a tool-proxy protocol adapter.

    The standalone package intentionally does not import py_utils. The
    tool-proxy app adapter owns protocol I/O and calls these handlers.
    """
    # Mutable ref so attach_browser can swap the controller
    controller_ref: list[Any] = [None]
    # Camoufox session ref — when not None, standard tools route through it
    camoufox_ref: list[Any] = [None]
    # Populated when multiple profiles are live and no session is configured.
    # Non-session tools fail loudly until the agent picks one.
    live_profile_conflict: list[Any] = [None]

    def create_session():
        override = create_session_override_controller()
        if override is not None:
            controller_ref[0] = override
            return override

        preferred = create_project_preferred_controller()
        if preferred is not None:
            controller_ref[0] = preferred
            return preferred

        attached = load_active_attach_controller()
        if attached is not None:
            controller_ref[0] = attached
            return attached

        c, conflict = select_default_controller()
        controller_ref[0] = c
        if conflict is not None:
            live_profile_conflict[0] = conflict
        elif not c.isolated and c.profile:
            print(
                f"[browser-tools] Auto-attached to sole live profile '{c.profile}'",
                file=sys.stderr,
            )
        return c

    def call_tool(controller: PersistentChromeController, tool: str, args: Any) -> dict[str, Any]:
        # --- Camoufox-exclusive tools ---
        if tool == "launch_camoufox":
            return _handle_launch_camoufox(camoufox_ref, args)
        if tool == "close_camoufox":
            return _handle_close_camoufox(camoufox_ref)
        if tool in ("wait_for_human", "get_cookies"):
            cfox = camoufox_ref[0]
            if cfox is None:
                return {
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Error: {tool} requires an active Camoufox session. Call launch_camoufox first.",
                            }
                        ],
                        "isError": True,
                    }
                }
            return _camoufox_result_to_mcp(cfox.call_tool(tool, args))

        # --- Standard tools routed through Camoufox when active ---
        cfox = camoufox_ref[0]
        if cfox is not None and tool in _CAMOUFOX_TOOL_MAP:
            mapped = _CAMOUFOX_TOOL_MAP[tool]
            if mapped is None:
                return {
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Tool '{tool}' is not supported in Camoufox mode. Use the camoufox-specific equivalent.",
                            }
                        ],
                        "isError": True,
                    }
                }
            translated = _translate_args_for_camoufox(tool, args)
            return _camoufox_result_to_mcp(cfox.call_tool(mapped, translated))

        # --- Chrome/CDP tools (original path) ---
        if tool == "attach_browser":
            return handle_attach_browser(controller_ref, args)
        if tool == "use_browser_session":
            return handle_use_browser_session(controller_ref, args)
        if tool == "browser_session_status":
            return handle_browser_session_status(args)
        if tool == "list_profiles":
            return handle_list_profiles(args)
        if tool == "delete_profile":
            return handle_delete_profile(args)
        # If we detected multiple live profiles at session creation and the
        # agent hasn't picked one yet (attach_browser/use_browser_session would
        # have cleared the conflict by swapping controller_ref or saving an
        # override), refuse non-session tools rather than silently spawning a
        # new headless Chrome.
        if live_profile_conflict[0] is not None and controller_ref[0] is controller:
            return _format_live_profile_conflict_error(live_profile_conflict[0])
        # Use latest controller (may have been swapped by attach_browser)
        active = controller_ref[0] or controller
        return active.invoke_tool(tool, args)  # type: ignore[arg-type]

    return create_session, call_tool


def main():
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # High-level helper
    if args.command == "screenshot-url":
        return execute_screenshot_url(args)

    # Direct tool mappings
    tool_mappings = {
        "new-page": ("new_page", lambda: {"url": args.url}),
        "navigate": (
            "navigate_page",
            lambda: {"type": args.type, **(({"url": args.url}) if args.type == "url" else {})},  # type: ignore[reportUnknownLambdaType]
        ),
        "list-pages": ("list_pages", lambda: {}),  # type: ignore[reportUnknownLambdaType]
        "select-page": ("select_page", lambda: {"pageId": args.page_id}),
        "take-snapshot": (
            "take_snapshot",
            lambda: {
                **({"verbose": True} if args.verbose else {}),
                **({"filePath": args.file_path} if args.file_path else {}),
            },
        ),
        "take-screenshot": (
            "take_screenshot",
            lambda: {
                **({"uid": args.uid} if args.uid else {}),
                **({"fullPage": True} if args.full_page else {}),
                **({"filePath": args.file_path} if args.file_path else {}),
                **({"format": args.format_type} if args.format_type else {}),
                **({"quality": args.quality} if args.quality else {}),
            },
        ),
        "wait-for": ("wait_for", lambda: {"text": args.text}),
        "click": (
            "click",
            lambda: {"uid": args.uid, **({"dblClick": True} if args.dbl_click else {})},
        ),
        "fill": ("fill", lambda: {"uid": args.uid, "value": args.value}),
        "evaluate": ("evaluate_script", lambda: {"function": args.function}),
        "press-key": ("press_key", lambda: {"key": args.key}),
        "resize": ("resize_page", lambda: {"width": args.width, "height": args.height}),
        "console-list": (
            "list_console_messages",
            lambda: {**({"pageSize": args.page_size} if args.page_size else {})},
        ),
        "network-list": (
            "list_network_requests",
            lambda: {**({"pageSize": args.page_size} if args.page_size else {})},
        ),
        "perf-start": (
            "performance_start_trace",
            lambda: {**({"reload": True} if args.reload else {})},
        ),
        "perf-stop": ("performance_stop_trace", lambda: {}),  # type: ignore[reportUnknownLambdaType]
    }

    if args.command in tool_mappings:
        tool_name, param_builder = tool_mappings[args.command]
        params = param_builder()
        return execute_direct_tool(tool_name, params, args)

    # Local profiler commands (bypass MCP, use CDP directly)
    if args.command == "profiler-timed":
        return execute_profiler_timed(args)
    if args.command == "profiler-watch":
        return execute_profiler_watch(args)

    error_exit(f"Unknown command: {args.command}")


def _handle_launch_camoufox(camoufox_ref: list[Any], args: Any) -> dict[str, Any]:
    """Launch a Camoufox anti-detect browser session.

    Args:
        camoufox_ref: Single-element list holding the active CamoufoxSession.
        args: Tool arguments (headless, proxy, os).

    Returns:
        MCP-style response.
    """
    from .camoufox_session import CamoufoxSession

    if camoufox_ref[0] is not None:
        return {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Camoufox is already running. Call close_camoufox first to restart.",
                    }
                ]
            }
        }

    session = CamoufoxSession()
    result = session.call_tool("launch_browser", args)

    if "error" in result:
        return {
            "result": {
                "content": [
                    {"type": "text", "text": f"Error launching Camoufox: {result['error']}"}
                ],
                "isError": True,
            }
        }

    camoufox_ref[0] = session

    lines = [
        "🦊 Camoufox anti-detect browser launched.",
        f"  Fingerprint: {result['result'].get('fingerprint', 'unknown')}",
        "",
        "Standard tools (navigate_page, click, fill, take_screenshot, etc.)",
        "now route through Camoufox instead of Chrome.",
        "",
        "Call close_camoufox to switch back to Chrome.",
    ]
    return {"result": {"content": [{"type": "text", "text": "\n".join(lines)}]}}


def _handle_close_camoufox(camoufox_ref: list[Any]) -> dict[str, Any]:
    """Close the Camoufox session and switch back to Chrome.

    Args:
        camoufox_ref: Single-element list holding the active CamoufoxSession.

    Returns:
        MCP-style response.
    """
    session = camoufox_ref[0]
    if session is None:
        return {"result": {"content": [{"type": "text", "text": "No Camoufox session is active."}]}}

    session.call_tool("close_browser", {})
    camoufox_ref[0] = None

    return {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "Camoufox closed. Standard tools now route through Chrome again.",
                }
            ]
        }
    }


if __name__ == "__main__":
    sys.exit(main())
