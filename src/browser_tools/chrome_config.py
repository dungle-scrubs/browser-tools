"""
Browser Tools MCP Configuration
Defines ACTUAL tool schemas from chrome-devtools-mcp repository
"""

# Tool definitions organized by category
TOOL_CATEGORIES = {
    "navigation": [
        "navigate_page",
        "new_page",
        "close_page",
        "list_pages",
        "select_page",
        "wait_for",
    ],
    "input": [
        "click",
        "drag",
        "fill",
        "fill_form",
        "handle_dialog",
        "hover",
        "press_key",
        "upload_file",
    ],
    "emulation": ["emulate", "resize_page"],
    "performance": [
        "performance_start_trace",
        "performance_stop_trace",
        "performance_analyze_insight",
    ],
    "network": ["list_network_requests", "get_network_request"],
    "debugging": [
        "evaluate_script",
        "take_screenshot",
        "take_snapshot",
        "list_console_messages",
        "get_console_message",
    ],
    "browser_management": [
        "attach_browser",
        "list_profiles",
        "delete_profile",
    ],
    "frames": [
        "list_frames",
        "select_frame",
        "reset_frame",
        "get_frame_storage",
        "get_frame_events",
    ],
    "accessibility": [
        "ax_find",
        "ax_node",
    ],
    "waits": [
        "wait_idle",
        "wait_stable",
    ],
    "content": [
        "get_text",
        "get_html",
        "get_attr",
    ],
    "export": [
        "export_pdf",
        "screenshot_element",
        "screencast_start",
        "screencast_stop",
    ],
    "element_query": [
        "element_exists",
        "element_visible",
    ],
}

# Tool parameter schemas (ACTUAL from chrome-devtools-mcp GitHub)
# Format: tool_name -> {param_name: (type, required, description)}
TOOL_SCHEMAS = {
    # Navigation tools
    "navigate_page": {
        "type": (str, False, "Navigate type: 'url', 'back', 'forward', 'reload'"),
        "url": (str, False, "Target URL (required when type='url')"),
        "ignoreCache": (bool, False, "Ignore cache on reload"),
        "timeout": (int, False, "Navigation timeout in ms"),
    },
    "new_page": {"url": (str, True, "URL to load in the new page")},
    "close_page": {"pageId": (int, True, "Page ID to close (from list_pages)")},
    "list_pages": {},  # No parameters
    "select_page": {"pageId": (int, True, "Page ID to select (from list_pages)")},
    "wait_for": {"text": (str, True, "Text to wait for on the page")},
    # Input automation tools (ALL use UIDs from snapshots)
    "click": {
        "uid": (str, True, "Element UID from page snapshot"),
        "dblClick": (bool, False, "Set to true for double click"),
    },
    "drag": {
        "from_uid": (str, True, "UID of element to drag"),
        "to_uid": (str, True, "UID of drop target"),
    },
    "fill": {
        "uid": (str, True, "Element UID from page snapshot"),
        "value": (str, True, "Value to fill"),
    },
    "fill_form": {"elements": (list, True, "Array of {uid, value} objects")},
    "handle_dialog": {
        "action": (str, True, "Action: 'accept' or 'dismiss'"),
        "promptText": (str, False, "Text to enter in prompt dialog"),
    },
    "hover": {"uid": (str, True, "Element UID from page snapshot")},
    "press_key": {"key": (str, True, "Key or combination (e.g., 'Enter', 'Control+A')")},
    "upload_file": {
        "uid": (str, True, "Element UID from page snapshot"),
        "filePath": (str, True, "Local path to file"),
    },
    # Emulation tools
    "emulate": {
        "networkConditions": (str, False, "Network throttling preset"),
        "cpuThrottlingRate": (int, False, "CPU slowdown factor (1-20)"),
    },
    "resize_page": {
        "width": (int, True, "Page width in pixels"),
        "height": (int, True, "Page height in pixels"),
    },
    # Performance tools
    "performance_start_trace": {
        "reload": (bool, False, "Reload page once tracing starts"),
        "autoStop": (bool, False, "Auto-stop trace recording"),
    },
    "performance_stop_trace": {},  # No parameters
    "performance_analyze_insight": {
        "insightSetId": (str, True, "Insight set ID from trace results"),
        "insightName": (str, True, "Insight name (e.g., 'LCPBreakdown')"),
    },
    # Network tools
    "list_network_requests": {
        "pageSize": (int, False, "Max requests to return"),
        "pageIdx": (int, False, "Page number (0-based)"),
        "resourceTypes": (list, False, "Filter by resource types"),
        "includePreservedRequests": (bool, False, "Include last 3 navigations"),
    },
    "get_network_request": {"reqid": (int, False, "Request ID (omit for selected request)")},
    # Debugging tools
    "evaluate_script": {
        "function": (str, True, "JavaScript function declaration"),
        "args": (list, False, "Array of {uid} objects for function args"),
    },
    "take_screenshot": {
        "format": (str, False, "Image format: 'png', 'jpeg', 'webp'"),
        "quality": (int, False, "JPEG/WebP quality (0-100)"),
        "uid": (str, False, "Element UID to screenshot"),
        "fullPage": (bool, False, "Capture full scrollable page"),
        "filePath": (str, False, "Path to save screenshot"),
    },
    "take_snapshot": {
        "verbose": (bool, False, "Include full a11y tree details"),
        "filePath": (str, False, "Path to save snapshot"),
    },
    "list_console_messages": {
        "pageSize": (int, False, "Max messages to return"),
        "pageIdx": (int, False, "Page number (0-based)"),
        "types": (list, False, "Filter by message types"),
        "includePreservedMessages": (bool, False, "Include last 3 navigations"),
    },
    "get_console_message": {"msgid": (int, True, "Console message ID")},
    # Browser management tools
    "attach_browser": {
        "endpoint": (str, True, "Chrome remote debugging endpoint URL"),
        "tab_url": (str, False, "URL substring to auto-select a tab"),
        "profile": (str, False, "Named profile for persistent sessions"),
        "mode": (str, False, "Access mode: 'full' or 'inspect'"),
    },
    "list_profiles": {},
    "delete_profile": {
        "name": (str, True, "Profile name to delete"),
    },
    # Frame tools
    "list_frames": {},
    "select_frame": {
        "url_pattern": (str, True, "URL substring to match frame URLs"),
    },
    "reset_frame": {},
    "get_frame_storage": {
        "storage_types": (list, False, "Storage types to retrieve"),
    },
    "get_frame_events": {},
    # Accessibility tools (CDP Accessibility domain)
    "ax_find": {
        "role": (str, False, "Accessibility role (e.g. button, link, heading, textbox)"),
        "name": (str, False, "Accessible name (partial match, case-insensitive)"),
    },
    "ax_node": {
        "selector": (str, True, "CSS selector for the element"),
    },
    # Semantic wait tools (JS injection)
    "wait_idle": {
        "timeout_ms": (int, False, "Max wait in ms (default: 5000)"),
        "idle_ms": (int, False, "Network silence duration to consider idle (default: 500)"),
    },
    "wait_stable": {
        "timeout_ms": (int, False, "Max wait in ms (default: 5000)"),
        "stable_ms": (int, False, "DOM quiescence duration in ms (default: 300)"),
    },
    # Content extraction tools
    "get_text": {"selector": (str, True, "CSS selector")},
    "get_html": {"selector": (str, True, "CSS selector")},
    "get_attr": {
        "selector": (str, True, "CSS selector"),
        "attribute": (str, True, "Attribute name (e.g. href, src, data-id)"),
    },
    # Export tools (CDP Page domain)
    "export_pdf": {
        "path": (str, False, "Output file path"),
        "landscape": (bool, False, "Landscape orientation"),
        "print_background": (bool, False, "Print background graphics"),
    },
    "screenshot_element": {
        "selector": (str, True, "CSS selector for element to capture"),
        "path": (str, False, "Output file path"),
    },
    "screencast_start": {
        "format": (str, False, "Frame format: 'jpeg' (default) or 'png'"),
        "quality": (int, False, "JPEG quality 0-100 (default 80)"),
        "every_nth_frame": (int, False, "Capture every Nth painted frame (default 1)"),
        "max_frames": (int, False, "Max frames to buffer before pausing (default 600)"),
        "max_width": (int, False, "Downscale frames to this max width"),
        "max_height": (int, False, "Downscale frames to this max height"),
    },
    "screencast_stop": {
        "dir": (str, True, "Directory to write timestamped frames + frames.json"),
    },
    # Element query tools
    "element_exists": {"selector": (str, True, "CSS selector")},
    "element_visible": {"selector": (str, True, "CSS selector")},
}

# Chrome launch configuration
CHROME_CONFIG = {
    "default_viewport": {"width": 1280, "height": 720},
    "default_timeout": 30000,  # 30 seconds
    "headless": False,
    "isolated": False,
    "channel": "canary",  # ALWAYS use canary, not stable
}


# MCP server command configuration
def get_mcp_command(
    headless=False, isolated=False, viewport=None, channel="canary", browser_url=None
):
    """
    Build MCP server command with options

    Args:
        headless: Run Chrome without UI
        isolated: Use temporary user data directory
        viewport: Viewport dimensions as "WIDTHxHEIGHT" string
        channel: Chrome channel (stable, canary, beta, dev)
        browser_url: URL to connect to existing Chrome instance (e.g., http://localhost:9222)

    Returns:
        List of command arguments
    """
    cmd = ["npx", "-y", "chrome-devtools-mcp@latest"]

    if browser_url:
        # Connect to existing Chrome instance - ignore other launch options
        cmd.extend(["--browserUrl", browser_url])
    else:
        # Launch new Chrome instance
        if headless:
            cmd.append("--headless")

        if isolated:
            cmd.append("--isolated")

        if viewport:
            cmd.extend(["--viewport", viewport])

        if channel != "stable":
            cmd.extend(["--channel", channel])

    return cmd


# Helper function to get all tools
def get_all_tools():
    """Get list of all available tool names"""
    all_tools = []
    for category_tools in TOOL_CATEGORIES.values():
        all_tools.extend(category_tools)
    return all_tools


# Helper function to get tool category
def get_tool_category(tool_name):
    """Get category for a given tool name"""
    for category, tools in TOOL_CATEGORIES.items():
        if tool_name in tools:
            return category
    return None


# Helper function to validate tool parameters
def validate_tool_params(tool_name, params):
    """
    Validate parameters for a tool

    Args:
        tool_name: Name of the tool
        params: Dictionary of parameters

    Returns:
        Tuple of (is_valid, error_message)
    """
    if tool_name not in TOOL_SCHEMAS:
        return False, f"Unknown tool: {tool_name}"

    schema = TOOL_SCHEMAS[tool_name]

    # Check required parameters
    for param_name, (param_type, required, description) in schema.items():
        if required and param_name not in params:
            return False, f"Missing required parameter: {param_name} ({description})"

    # Check parameter types (basic validation)
    for param_name, value in params.items():
        if param_name not in schema:
            # Allow unknown parameters (MCP might have additional ones)
            continue

        expected_type, _, _ = schema[param_name]
        if not isinstance(value, expected_type):
            return (
                False,
                f"Invalid type for {param_name}: expected {expected_type.__name__}, got {type(value).__name__}",
            )

    return True, None


# Helper function to get tool description
def get_tool_description(tool_name):
    """Get human-readable description of a tool"""
    descriptions = {
        # Navigation
        "navigate_page": "Navigate to URL or back/forward/reload",
        "new_page": "Create a new browser page/tab",
        "close_page": "Close a browser page by index",
        "list_pages": "List all open pages",
        "select_page": "Switch to a specific page by index",
        "wait_for": "Wait for text to appear on page",
        # Input (all use UIDs from snapshots)
        "click": "Click an element by UID",
        "drag": "Drag and drop elements by UID",
        "fill": "Fill an input field by UID",
        "fill_form": "Fill multiple form fields by UID",
        "handle_dialog": "Accept/dismiss browser dialogs",
        "hover": "Hover over an element by UID",
        "press_key": "Press keyboard keys or combinations",
        "upload_file": "Upload files by element UID",
        # Emulation
        "emulate": "Emulate network and CPU conditions",
        "resize_page": "Resize viewport dimensions",
        # Performance
        "performance_start_trace": "Start recording performance trace",
        "performance_stop_trace": "Stop recording trace",
        "performance_analyze_insight": "Get detailed performance insights",
        # Network
        "list_network_requests": "List captured network requests",
        "get_network_request": "Get details of a specific request",
        # Debugging
        "evaluate_script": "Execute JavaScript function in page",
        "take_screenshot": "Capture screenshot of page or element",
        "take_snapshot": "Capture accessibility tree snapshot",
        "list_console_messages": "List console messages",
        "get_console_message": "Get console message details",
        # Browser management
        "attach_browser": "Attach to existing Chrome with remote debugging",
        "list_profiles": "List saved browser profiles",
        "delete_profile": "Delete a named browser profile",
        # Frames
        "list_frames": "List all frames (iframes) in the page",
        "select_frame": "Select an iframe by URL pattern",
        "reset_frame": "Return to top-level frame context",
        "get_frame_storage": "Get cookies/storage for selected frame",
        "get_frame_events": "Get buffered frame navigation events",
        # Accessibility
        "ax_find": "Find accessibility nodes by role and/or name",
        "ax_node": "Inspect a single element's accessibility properties",
        # Waits
        "wait_idle": "Wait for network to go idle",
        "wait_stable": "Wait for DOM mutations to stop",
        # Content extraction
        "get_text": "Get text content of element by CSS selector",
        "get_html": "Get outer HTML of element by CSS selector",
        "get_attr": "Get attribute value of element by CSS selector",
        # Export
        "export_pdf": "Export current page as PDF file",
        "screenshot_element": "Screenshot a specific element by CSS selector",
        "screencast_start": "Start recording every painted frame (catches transient states like loading spinners that take_screenshot misses)",
        "screencast_stop": "Stop the screencast and write buffered frames + a manifest to a directory",
        # Element queries
        "element_exists": "Check if element exists in the DOM",
        "element_visible": "Check if element is visible",
    }
    return descriptions.get(tool_name, "No description available")
