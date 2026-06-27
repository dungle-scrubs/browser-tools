"""
Chrome DevTools MCP Utilities
Helper functions for JSON processing, subprocess management, and error handling
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable


class BrowserToolsError(Exception):
    """Base exception for Chrome DevTools wrapper errors"""

    pass


class MCPInvocationError(BrowserToolsError):
    """Error invoking MCP tool"""

    pass


class ParameterValidationError(BrowserToolsError):
    """Error validating tool parameters"""

    pass


def invoke_mcp_tool(
    tool_name: str, params: dict[str, Any], config: list[str] | None = None
) -> dict[str, Any]:
    """
    Invoke Browser Tools MCP tool via MCP Inspector CLI

    Args:
        tool_name: Name of the MCP tool to invoke
        params: Dictionary of tool parameters
        config: Optional MCP server command configuration

    Returns:
        Dictionary containing tool response

    Raises:
        MCPInvocationError: If tool invocation fails
    """
    # Build MCP Inspector command
    cmd = ["npx", "@modelcontextprotocol/inspector", "--cli"]

    # Add MCP server command (default or custom)
    if config:
        cmd.extend(config)
    else:
        cmd.extend(["npx", "-y", "chrome-devtools-mcp@latest"])

    # Add method and tool name
    cmd.extend(["--method", "tools/call", "--tool-name", tool_name])

    # Add parameters
    for key, value in params.items():
        # Convert value to string representation
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value)
        elif isinstance(value, bool):
            value_str = "true" if value else "false"
        else:
            value_str = str(value)

        cmd.extend(["--tool-arg", f"{key}={value_str}"])

    # Execute command
    try:
        import os

        env = os.environ.copy()
        env["MCP_AUTO_OPEN_ENABLED"] = "false"  # Don't auto-open browser for inspector

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,  # 60 second timeout
            env=env,
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            raise MCPInvocationError(f"MCP tool invocation failed: {error_msg}")

        # Parse JSON response
        try:
            response = json.loads(result.stdout)
            return response
        except json.JSONDecodeError as e:
            raise MCPInvocationError(f"Invalid JSON response: {e}\nOutput: {result.stdout}") from e

    except subprocess.TimeoutExpired as e:
        raise MCPInvocationError("Tool invocation timed out after 60 seconds") from e
    except FileNotFoundError as e:
        raise MCPInvocationError(
            "npx command not found. Please ensure Node.js is installed."
        ) from e
    except OSError as e:
        raise MCPInvocationError(f"Unexpected error invoking tool: {e}") from e


def list_mcp_tools(config: list[str] | None = None) -> list[dict[str, Any]]:
    """
    List available MCP tools

    Args:
        config: Optional MCP server command configuration

    Returns:
        List of tool definitions

    Raises:
        MCPInvocationError: If listing tools fails
    """
    # Build command
    cmd = ["npx", "@modelcontextprotocol/inspector", "--cli"]

    if config:
        cmd.extend(config)
    else:
        cmd.extend(["npx", "-y", "chrome-devtools-mcp@latest"])

    cmd.extend(["--method", "tools/list"])

    # Execute
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise MCPInvocationError(f"Failed to list tools: {result.stderr}")

        response = json.loads(result.stdout)
        return response.get("tools", [])

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        raise MCPInvocationError(f"Error listing tools: {e}") from e


def extract_content(response: dict[str, Any]) -> str:
    """
    Extract text content from MCP tool response.

    Args:
        response: MCP tool response dictionary.

    Returns:
        Extracted text content.
    """
    payload: Any = response
    if isinstance(response.get("result"), dict):
        payload = response["result"]

    if isinstance(payload, dict) and "content" in payload:
        content_items = payload["content"]
        if isinstance(content_items, list):
            # Extract text from all content items
            text_parts = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            return "\n".join(text_parts)
        if isinstance(content_items, str):
            return content_items

    # If no content found, return JSON representation
    return json.dumps(response, indent=2)


def format_response(response: dict[str, Any], format_type: str = "text") -> str:
    """
    Format MCP response for output

    Args:
        response: MCP tool response
        format_type: Output format ('text', 'json', 'pretty')

    Returns:
        Formatted string
    """
    if format_type == "json":
        return json.dumps(response, indent=None)
    elif format_type == "pretty":
        return json.dumps(response, indent=2)
    else:  # text
        return extract_content(response)


def parse_viewport(viewport_str: str) -> tuple[int, int]:
    """
    Parse viewport string to width and height

    Args:
        viewport_str: Viewport as "WIDTHxHEIGHT" (e.g., "1280x720")

    Returns:
        Tuple of (width, height)

    Raises:
        ParameterValidationError: If format is invalid
    """
    try:
        parts = viewport_str.lower().split("x")
        if len(parts) != 2:
            raise ValueError("Invalid format")

        width = int(parts[0])
        height = int(parts[1])

        if width <= 0 or height <= 0:
            raise ValueError("Dimensions must be positive")

        return width, height

    except (ValueError, TypeError) as exc:
        raise ParameterValidationError(
            f"Invalid viewport format '{viewport_str}'. "
            f"Expected format: WIDTHxHEIGHT (e.g., 1280x720).\n{exc}"
        ) from exc


def parse_json_param(json_str: str, param_name: str) -> Any:
    """
    Parse JSON parameter string

    Args:
        json_str: JSON string
        param_name: Parameter name (for error messages)

    Returns:
        Parsed JSON value

    Raises:
        ParameterValidationError: If JSON is invalid
    """
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ParameterValidationError(f"Invalid JSON for parameter '{param_name}': {e}") from e


def error_exit(message: str, code: int = 1) -> NoReturn:
    """
    Print error message and exit

    Args:
        message: Error message
        code: Exit code (default: 1)
    """
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


def success_output(message: str, data: Any | None = None, format_type: str = "text"):
    """
    Print success output

    Args:
        message: Success message
        data: Optional data to output
        format_type: Output format ('text', 'json', 'pretty')
    """
    if format_type == "json":
        output = {"success": True, "message": message}
        if data is not None:
            output["data"] = data
        print(json.dumps(output))
    else:
        print(message)
        if data is not None:
            if isinstance(data, (dict, list)):
                if format_type == "pretty":
                    print(json.dumps(data, indent=2))
                else:
                    print(data)
            else:
                print(data)


def validate_file_path(path: str, must_exist: bool = False) -> str:
    """
    Validate file path

    Args:
        path: File path to validate
        must_exist: Whether file must already exist

    Returns:
        Validated path

    Raises:
        ParameterValidationError: If path is invalid
    """
    import os

    # Expand user home directory
    path = os.path.expanduser(path)

    if must_exist and not os.path.exists(path):
        raise ParameterValidationError(f"File does not exist: {path}")

    # Ensure parent directory exists for new files
    if not must_exist:
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.exists(parent_dir):
            raise ParameterValidationError(f"Parent directory does not exist: {parent_dir}")

    return path


def retry_on_failure(func: Callable[..., Any], max_retries: int = 3, delay: float = 1.0):
    """
    Retry a function on failure

    Args:
        func: Function to execute
        max_retries: Maximum number of retries
        delay: Delay between retries in seconds

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    import time

    last_exception = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2  # Exponential backoff

    if last_exception is not None:
        raise last_exception
    raise RuntimeError("Retry failed with no exception")


# ============================================================================
# Snapshot Parsing Utilities
# ============================================================================


def parse_snapshot(snapshot_text: str) -> list[dict[str, str]]:
    """
    Parse accessibility tree snapshot into structured elements

    Args:
        snapshot_text: Raw snapshot text from take_snapshot

    Returns:
        List of element dictionaries with uid, role, name, value, etc.
    """
    elements = []

    for line in snapshot_text.strip().split("\n"):
        if not line.strip():
            continue

        # Example format: "  123 button 'Submit Form' disabled"
        # Parse: [indent] [uid] [role] ['name'] [attributes...]
        parts = line.strip().split(maxsplit=2)

        if len(parts) < 2:
            continue

        try:
            uid = parts[0]
            role = parts[1] if len(parts) > 1 else ""
            rest = parts[2] if len(parts) > 2 else ""

            # Extract name from quotes if present
            name = ""
            if "'" in rest:
                start = rest.index("'")
                end = rest.index("'", start + 1) if rest.count("'") > 1 else len(rest)
                name = rest[start + 1 : end]
                rest = rest[end + 1 :].strip()

            elements.append(
                {"uid": uid, "role": role, "name": name, "attributes": rest, "raw": line}
            )
        except (ValueError, IndexError):
            # Skip malformed lines
            continue

    return elements


def find_element_uid(
    snapshot_text: str,
    text: str | None = None,
    role: str | None = None,
    name: str | None = None,
    case_sensitive: bool = False,
) -> str | None:
    """
    Find element UID from snapshot by text/role/name

    Args:
        snapshot_text: Raw snapshot text
        text: Text to search for in element name
        role: Element role (button, textbox, link, etc.)
        name: Exact element name match
        case_sensitive: Case-sensitive matching

    Returns:
        Element UID or None if not found
    """
    elements = parse_snapshot(snapshot_text)

    for elem in elements:
        # Check role match
        if role and elem["role"].lower() != role.lower():
            continue

        # Check exact name match
        if name:
            if case_sensitive:
                if elem["name"] != name:
                    continue
            else:
                if elem["name"].lower() != name.lower():
                    continue

        # Check text substring match
        if text:
            if case_sensitive:
                if text not in elem["name"]:
                    continue
            else:
                if text.lower() not in elem["name"].lower():
                    continue

        # All criteria matched
        return elem["uid"]

    return None


def find_all_element_uids(
    snapshot_text: str, text: str | None = None, role: str | None = None
) -> list[str]:
    """
    Find all element UIDs matching criteria

    Args:
        snapshot_text: Raw snapshot text
        text: Text to search for in element name
        role: Element role filter

    Returns:
        List of matching UIDs
    """
    elements = parse_snapshot(snapshot_text)
    matches = []

    for elem in elements:
        # Check role match
        if role and elem["role"].lower() != role.lower():
            continue

        # Check text match
        if text and text.lower() not in elem["name"].lower():
            continue

        matches.append(elem["uid"])

    return matches


def get_element_info(snapshot_text: str, uid: str) -> dict[str, str] | None:
    """
    Get element information by UID

    Args:
        snapshot_text: Raw snapshot text
        uid: Element UID

    Returns:
        Element dict or None if not found
    """
    elements = parse_snapshot(snapshot_text)

    for elem in elements:
        if elem["uid"] == uid:
            return elem

    return None


def list_elements_by_role(snapshot_text: str, role: str) -> list[dict[str, str]]:
    """
    List all elements with specific role

    Args:
        snapshot_text: Raw snapshot text
        role: Element role (button, link, textbox, etc.)

    Returns:
        List of element dicts
    """
    elements = parse_snapshot(snapshot_text)
    return [elem for elem in elements if elem["role"].lower() == role.lower()]


def find_button(snapshot_text: str, text: str) -> str | None:
    """Convenience function to find button by text"""
    return find_element_uid(snapshot_text, text=text, role="button")


def find_link(snapshot_text: str, text: str) -> str | None:
    """Convenience function to find link by text"""
    return find_element_uid(snapshot_text, text=text, role="link")


def find_textbox(snapshot_text: str, name: str) -> str | None:
    """Convenience function to find textbox by name"""
    return find_element_uid(snapshot_text, name=name, role="textbox")


def format_snapshot_summary(snapshot_text: str, max_elements: int = 20) -> str:
    """
    Format snapshot into human-readable summary

    Args:
        snapshot_text: Raw snapshot text
        max_elements: Maximum elements to show

    Returns:
        Formatted summary string
    """
    elements = parse_snapshot(snapshot_text)

    if not elements:
        return "No elements found in snapshot"

    summary = [f"Found {len(elements)} elements:\n"]

    for _i, elem in enumerate(elements[:max_elements]):
        summary.append(f"  [{elem['uid']}] {elem['role']}: '{elem['name']}'")

    if len(elements) > max_elements:
        summary.append(f"\n  ... and {len(elements) - max_elements} more")

    return "\n".join(summary)
