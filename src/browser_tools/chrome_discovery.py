"""Find a Chrome started for debugging, from the user data directory it runs on.

What ``--chrome-profile`` takes is a **user data directory path**, because that
is the only thing that can work. Measured on the driving dev's Chrome 153,
against their real default profile:

    $ "Google Chrome" --headless=new \\
        --user-data-dir="$HOME/Library/Application Support/Google/Chrome" \\
        --remote-debugging-port=17492 about:blank

    DevTools remote debugging requires a non-default data directory. Specify
    this using --user-data-dir.

No port file, no endpoint. The flag's first shape took a channel name and
resolved it to that channel's platform default directory, and on macOS and
Windows there were no environment overrides, so the only address it could
produce was the one Chrome declines. It had no working configuration. A channel
name is still accepted, because on Linux the environment can move a channel's
directory somewhere Chrome will debug; when a name resolves to a platform
default the flag refuses and says how to start a Chrome it can reach.

Verifying which family is Chrome, rather than telling both
----------------------------------------------------------
``DevToolsActivePort`` holds the listening port on line 1 and the browser
WebSocket path on line 2. That path carries Chrome's browser GUID, and the GUID
is the whole authentication of the browser WebSocket: anything that knows it has
full unauthenticated control of the browser.

Chrome records a port number and not an address family, and a preferred port
held on IPv4 makes Chrome bind the same number on IPv6, so a client must
consider both families. The first version sent the GUID path to both. Measured:
a plain non-CDP listener on the IPv4 side logged
``GET /devtools/browser/2732e1f6-...``, so every attach handed the browser's
capability token to an unrelated process; and with a CDP-speaking impostor on
IPv4 and Chrome on IPv6, ten runs gave the impostor two of them.

So the GUID is used to **verify** instead of to introduce. Each family is asked
for ``/json/version`` over plain HTTP, which carries no secret, and the
``webSocketDebuggerUrl`` path in the reply is compared with line 2 of the port
file. Only a peer that already knows the GUID is the Chrome that wrote the file,
and only that peer is dialled. The leak is gone, the coin flip is gone, and a
stale port file left behind by a dead Chrome now fails with a diagnostic instead
of delivering ``Page.navigate`` to whoever inherited the port.
"""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .endpoint import EndpointUsageError, ResolvedEndpoint, resolve_endpoint_port

CHANNELS = ("stable", "beta", "dev", "canary")

#: The loopback families a recorded port may be listening on, in the order they
#: are probed. Both are tried because Chrome records a port and not a family.
LOOPBACK_FAMILIES = ("127.0.0.1", "[::1]")

#: What to do instead, when the resolved directory is one Chrome refuses.
START_A_DEBUGGING_CHROME = (
    'Start Chrome on a directory of its own:\n'
    '  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\\n'
    '    --user-data-dir="$HOME/.local/share/bt-debug-profile" \\\n'
    '    --remote-debugging-port=0\n'
    "then: bt snapshot --chrome-profile ~/.local/share/bt-debug-profile"
)


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


def _default_directory(channel: str) -> Path | None:
    """The channel's platform default, ignoring any environment override.

    The refusal is about Chrome's *computed* default. On Linux
    ``CHROME_USER_DATA_DIR`` and ``CHROME_CONFIG_HOME`` move where bt looks but
    do not move where Chrome's own default is, so the comparison is made
    against the unmoved path.
    """
    index = CHANNELS.index(channel)
    if sys.platform == "darwin":
        name = ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome Canary")[index]
        return Path.home() / "Library/Application Support/Google" / name
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return None
        name = ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome SxS")[index]
        return Path(base) / "Google" / name / "User Data"
    if sys.platform.startswith("linux"):
        suffix = ("", "-beta", "-unstable", "-canary")[index]
        return Path.home() / ".config" / f"google-chrome{suffix}"
    return None


def _same_directory(left: Path, right: Path) -> bool:
    """Compare two directory paths that need not exist."""
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    try:
        return os.path.normcase(str(left.expanduser().resolve())) == os.path.normcase(
            str(right.expanduser().resolve())
        )
    except OSError:
        return False


def resolve_profile_directory(profile: str) -> Path:
    """The user data directory ``--chrome-profile`` names.

    Args:
        profile: A channel name, or a path to a directory Chrome was started
            on with ``--user-data-dir``.

    Returns:
        The directory to read ``DevToolsActivePort`` from.

    Raises:
        EndpointUsageError: The value is a channel name on a platform Chrome
            discovery does not know.
    """
    if profile in CHANNELS:
        return user_data_directory(profile)
    return Path(profile).expanduser()


def refuse_default_directory(directory: Path, profile: str) -> None:
    """Refuse a directory Chrome will not start remote debugging on.

    Raises:
        EndpointUsageError: ``directory`` is a channel's platform default.
    """
    for channel in CHANNELS:
        default = _default_directory(channel)
        if default is None or not _same_directory(directory, default):
            continue
        named = f"The Chrome {channel} channel's" if profile in CHANNELS else "That is the Chrome"
        raise EndpointUsageError(
            f"{named} default data directory ({directory}), and Chrome refuses remote "
            f"debugging there: \"DevTools remote debugging requires a non-default data "
            f"directory. Specify this using --user-data-dir.\" It writes no "
            f"DevToolsActivePort file and starts no endpoint, so there is nothing for "
            f"--chrome-profile to find. Your everyday Chrome cannot be attached to.\n"
            f"{START_A_DEBUGGING_CHROME}"
        )


def _policy_forbids(value: object) -> bool:
    """Whether a managed ``RemoteDebuggingAllowed`` value is an explicit no.

    A plist writes a boolean as ``<false/>`` and an integer zero as
    ``<integer>0</integer>``, and both mean the same thing to Chrome; JSON
    policy files do the same with ``false`` and ``0``. Testing ``is False``
    saw only the first spelling, so an integer-zero policy read as "no policy"
    and bt reported the wrong refusal. The Windows branch already compared
    against ``0``.
    """
    return isinstance(value, (bool, int)) and value == 0


def _macos_policy_paths() -> list[Path]:
    """Every managed-preferences plist macOS may hold the Chrome policy in.

    macOS puts a per-user managed domain under ``/Library/Managed
    Preferences/<user>/``. That is the path this machine actually has, and the
    first version read neither it nor anything inside it: it read only the
    computer-level file and a ``~/Library/Managed Preferences`` path that does
    not exist here.
    """
    root = Path("/Library/Managed Preferences")
    paths = [root / "com.google.Chrome.plist"]
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = ""
    if user:
        paths.insert(0, root / user / "com.google.Chrome.plist")
    paths.append(Path.home() / "Library/Managed Preferences/com.google.Chrome.plist")
    return paths


def remote_debugging_disallowed() -> bool:
    """Report only an explicit managed RemoteDebuggingAllowed=false policy.

    No Local State or browser credential store is read. Unknown policy state
    is not proof of permission; the missing-file diagnostic says so.
    """
    if sys.platform == "darwin":
        for path in _macos_policy_paths():
            try:
                data = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if isinstance(data, dict) and _policy_forbids(data.get("RemoteDebuggingAllowed")):
                return True
    elif sys.platform.startswith("linux"):
        for path in Path("/etc/opt/chrome/policies/managed").glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and _policy_forbids(data.get("RemoteDebuggingAllowed")):
                return True
    elif sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Policies\Google\Chrome") as key:
                    value, _ = winreg.QueryValueEx(key, "RemoteDebuggingAllowed")
                    if _policy_forbids(value):
                        return True
            except OSError:
                continue
    return False


def _read_port_file(path: Path) -> tuple[int, str]:
    """Read ``DevToolsActivePort`` into ``(port, browser_websocket_path)``."""
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
        for host in LOOPBACK_FAMILIES:
            resolve_endpoint_port(f"ws://{host}:{port}{lines[1]}")
    except (ValueError, EndpointUsageError) as exc:
        raise EndpointUsageError(f"Malformed port file {path}: {exc}") from exc
    return port, lines[1]


def _family_knows_the_guid(host: str, port: int, browser_path: str) -> bool:
    """Whether the peer on this family already holds the port file's GUID.

    ``/json/version`` is a plain HTTP read and carries nothing secret, so an
    impostor learns only that something asked. It answers with the browser
    WebSocket URL it serves; the Chrome that wrote the port file answers with
    the path in that file, and nothing else can.
    """
    from .devtools_http import fetch_devtools_json

    try:
        document = fetch_devtools_json(host, port, "/json/version")
    except (OSError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    url = document.get("webSocketDebuggerUrl")
    if not isinstance(url, str):
        return False
    try:
        return urlparse(url).path == browser_path
    except ValueError:
        return False


def discover_chrome(profile: str = "stable") -> ResolvedEndpoint:
    """Resolve one verified browser WebSocket from a Chrome debugging profile.

    Args:
        profile: A user data directory path, or a channel name.

    Returns:
        The endpoint for the single loopback family that proved it is the
        Chrome that wrote the port file.

    Raises:
        EndpointUsageError: The directory is one Chrome refuses, policy forbids
            debugging, the port file is missing or malformed, or neither
            loopback family proved itself (CLI exit 2).
    """
    directory = resolve_profile_directory(profile)
    refuse_default_directory(directory, profile)
    path = directory / "DevToolsActivePort"
    if remote_debugging_disallowed():
        raise EndpointUsageError(
            f"Chrome debugging is not permitted by RemoteDebuggingAllowed "
            f"(devtools.remote_debugging.allowed); ask the administrator. Port file: {path}"
        )
    port, browser_path = _read_port_file(path)
    # Both families are asked at once, so a family that accepts the connection
    # and never answers costs one timeout rather than two. The answer is still
    # deterministic: the results are read back in LOOPBACK_FAMILIES order.
    with ThreadPoolExecutor(max_workers=len(LOOPBACK_FAMILIES)) as pool:
        probes = [
            pool.submit(_family_knows_the_guid, host, port, browser_path)
            for host in LOOPBACK_FAMILIES
        ]
        verified = [host for host, probe in zip(LOOPBACK_FAMILIES, probes, strict=True) if probe.result()]
    for host in verified:
        url = f"ws://{host}:{port}{browser_path}"
        return ResolvedEndpoint(port, url, url, host, "--chrome-profile")
    raise EndpointUsageError(
        f"{path} records port {port}, but neither {LOOPBACK_FAMILIES[0]}:{port} nor "
        f"{LOOPBACK_FAMILIES[1]}:{port} answered /json/version with the browser path "
        f"that file holds. bt will not hand that path to a peer that does not already "
        f"know it, because it is the browser's whole authentication. Either the Chrome "
        f"that wrote the file has exited and the file is stale, or another process now "
        f"holds the port. Check who holds it with: lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
        f"A Chrome 144+ approval service answers 404 for /json/version by design and "
        f"cannot be discovered this way; pass its address yourself, which also makes "
        f"the family explicit: bt <verb> --endpoint ws://127.0.0.1:{port}{browser_path}"
    )
