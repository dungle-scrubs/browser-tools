"""Build the chrome-devtools-mcp subprocess command.

Tool routing (CDP vs. local vs. default) lives in ``tool_registry``; the MCP
subprocess advertises its own tool list and validates its own inputs, so a
parallel hand-maintained catalog/schema is not maintained here.
"""

from __future__ import annotations


def get_mcp_command(
    headless: bool = False,
    isolated: bool = False,
    viewport: str | None = None,
    channel: str = "canary",
    browser_url: str | None = None,
) -> list[str]:
    """Build the chrome-devtools-mcp subprocess command with launch options.

    Args:
        headless: Run Chrome without UI.
        isolated: Use a temporary user data directory.
        viewport: Viewport dimensions as ``WIDTHxHEIGHT``.
        channel: Chrome channel (stable, canary, beta, dev).
        browser_url: Connect to an existing Chrome instance (e.g.,
            ``http://localhost:9222``) instead of launching one.

    Returns:
        Command argument list for the MCP subprocess.
    """
    cmd = ["npx", "-y", "chrome-devtools-mcp@latest"]

    if browser_url:
        # Connect to an existing Chrome instance; other launch options are moot.
        cmd.extend(["--browserUrl", browser_url])
    else:
        if headless:
            cmd.append("--headless")

        if isolated:
            cmd.append("--isolated")

        if viewport:
            cmd.extend(["--viewport", viewport])

        if channel != "stable":
            cmd.extend(["--channel", channel])

    return cmd
