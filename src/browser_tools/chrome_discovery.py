"""Read Chrome's port file without registering or owning its profile."""

from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path

from .endpoint import EndpointUsageError, ResolvedEndpoint, resolve_endpoint_port

CHANNELS = ("stable", "beta", "dev", "canary")


def user_data_directory(channel: str) -> Path:
    """Chrome's desktop channel paths, per Chromium's user_data_dir.md."""
    if channel not in CHANNELS:
        raise EndpointUsageError(
            f"unknown Chrome channel {channel!r}; choose {', '.join(CHANNELS)}"
        )
    index = CHANNELS.index(channel)
    if sys.platform == "darwin":
        name = ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome Canary")[index]
        return Path.home() / "Library/Application Support/Google" / name
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise EndpointUsageError("Chrome discovery needs LOCALAPPDATA on Windows")
        name = ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome SxS")[index]
        return Path(base) / "Google" / name / "User Data"
    if sys.platform.startswith("linux"):
        override = os.environ.get("CHROME_USER_DATA_DIR")
        if override:
            return Path(override)
        base = os.environ.get("CHROME_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
        suffix = ("", "-beta", "-unstable", "-canary")[index]
        return (Path(base) if base else Path.home() / ".config") / f"google-chrome{suffix}"
    raise EndpointUsageError(f"Chrome discovery is unsupported on {sys.platform}")


def remote_debugging_disallowed() -> bool:
    """Report only an explicit managed RemoteDebuggingAllowed=false policy.

    No Local State or browser credential store is read. Unknown policy state
    is not proof of permission; the missing-file diagnostic says so.
    """
    if sys.platform == "darwin":
        paths = [
            Path("/Library/Managed Preferences/com.google.Chrome.plist"),
            Path.home() / "Library/Managed Preferences/com.google.Chrome.plist",
        ]
        for path in paths:
            try:
                data = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if isinstance(data, dict) and data.get("RemoteDebuggingAllowed") is False:
                return True
    elif sys.platform.startswith("linux"):
        for path in Path("/etc/opt/chrome/policies/managed").glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("RemoteDebuggingAllowed") is False:
                return True
    elif sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Policies\Google\Chrome") as key:
                    value, _ = winreg.QueryValueEx(key, "RemoteDebuggingAllowed")
                    if value == 0:
                        return True
            except OSError:
                continue
    return False


def discover_chrome(channel: str = "stable") -> ResolvedEndpoint:
    """Read the recorded port and browser path; missing files fail immediately."""
    path = user_data_directory(channel) / "DevToolsActivePort"
    if remote_debugging_disallowed():
        raise EndpointUsageError(
            f"Chrome debugging is not permitted by RemoteDebuggingAllowed "
            f"(devtools.remote_debugging.allowed); ask the administrator. Port file: {path}"
        )
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise EndpointUsageError(
            f"Cannot read {path}: debugging is not enabled or no endpoint was started. "
            "If the preferred port is occupied on both IPv4 and IPv6, Chrome keeps "
            "running without writing this file. Start Chrome for debugging on a "
            "non-default --user-data-dir with --remote-debugging-port=0. "
            "Check chrome://policy for RemoteDebuggingAllowed if enabling is not permitted."
        ) from exc
    except UnicodeError as exc:
        raise EndpointUsageError(
            f"Malformed port file {path}: expected port and browser path"
        ) from exc
    try:
        if len(lines) != 2 or not lines[0].isascii() or not lines[0].isdigit():
            raise ValueError("expected two lines: port and browser path")
        port = int(lines[0])
        urls = tuple(f"ws://{host}:{port}{lines[1]}" for host in ("127.0.0.1", "[::1]"))
        for url in urls:
            resolve_endpoint_port(url)
    except (ValueError, EndpointUsageError) as exc:
        raise EndpointUsageError(f"Malformed port file {path}: {exc}") from exc
    return ResolvedEndpoint(port, urls[0], urls)
