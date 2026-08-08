"""
Chrome DevTools MCP Utilities
Helper functions for JSON processing, subprocess management, and error handling
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, NoReturn

from .mcp_response import extract_text_items


class BrowserToolsError(Exception):
    """Base exception for Chrome DevTools wrapper errors"""

    pass


class MCPInvocationError(BrowserToolsError):
    """Error invoking MCP tool"""

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
            # Extract text from all content items via the canonical reader.
            return "\n".join(extract_text_items(response))
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
