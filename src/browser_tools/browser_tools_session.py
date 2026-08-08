#!/usr/bin/env python3
"""
Chrome DevTools MCP Wrapper
Simplified CLI for Chrome DevTools MCP with snapshot-based architecture
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Any

from .browser_session import (
    create_tool_proxy_handlers,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-exported for the tool-proxy adapter
)
from .chrome_config import get_mcp_command
from .chrome_utils import (
    BrowserToolsError,
    error_exit,
    format_response,
    invoke_mcp_tool,
    success_output,
)
from .persistent_browser import PersistentChromeController


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
    profiler_watch.add_argument(
        "--threshold", type=float, default=80.0, help="CPU threshold percentage"
    )
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


if __name__ == "__main__":
    sys.exit(main())
