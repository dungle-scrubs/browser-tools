"""Tool-proxy session adapter: routes MCP tool calls to the active browser.

Split out of the CLI (browser_tools_session). Owns session-tool dispatch
(attach/use_browser_session/close_browser), Camoufox routing, single-tab
enforcement, headless->headed auth-wall promotion, and live-profile conflict
resolution. The CLI stays in browser_tools_session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import persistent_browser, process_utils
from . import session_layout as layout
from .browser_state import (
    AUTH_MODES,
    HEADED_AUTH_MODES,
    HEADLESS_AUTH_MODES,
    ProjectBrowserConfig,
    normalize_mode,
)
from .live_chrome import resolve_live_chrome
from .mcp_response import error_response, text_response
from .persistent_browser import PersistentChromeController, format_dead_port_error
from .process_utils import validate_local_endpoint
from .profile_catalog import find_live_profiles
from .session_store import (
    clear_active_attach_config,
    clear_session_override,
    create_project_preferred_controller,
    create_session_override_controller,
    get_browser_session_status,
    load_active_attach_controller,
    load_project_browser_config,
    save_active_attach_config,
    save_session_override,
)


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
    endpoint = (args.get("endpoint") or "").strip() or None
    if endpoint is not None:
        endpoint_error = validate_local_endpoint(endpoint)
        if endpoint_error:
            return error_response(f"Error: {endpoint_error}")
    tab_url = args.get("tab_url")
    profile = args.get("profile")
    mode = args.get("mode", "full")
    stealth = args.get("stealth", False)

    discovered_pid: int | None = None
    if endpoint is None:
        if not profile:
            return error_response(
                "Error: pass either 'endpoint' (e.g. http://127.0.0.1:9222) or "
                "'profile' (a named profile from list_profiles). With profile alone, "
                "browser-tools discovers the running Chrome's debug port automatically."
            )
        profile_dir = layout.profile_dir(profile)
        if not profile_dir.exists():
            return error_response(
                f"Error: profile '{profile}' not found at {profile_dir}. "
                "Call list_profiles to see available profiles."
            )
        chrome = resolve_live_chrome(profile_dir)
        if chrome is None:
            return error_response(
                f"Error: profile '{profile}' has no running Chrome. "
                f"Either launch Chrome on this profile and retry, or call "
                f"use_browser_session(mode='headed-auth', profile='{profile}') to launch one."
            )
        if chrome.intended_port is None:
            return error_response(
                f"Error: Chrome (pid {chrome.pid}) holding profile '{profile}' has no "
                "--remote-debugging-port flag. Restart it with that flag, or kill it "
                f"and call use_browser_session(mode='headed-auth', profile='{profile}')."
            )
        if not chrome.devtools_alive:
            return error_response(format_dead_port_error(profile, profile_dir, chrome))
        assert chrome.endpoint is not None  # set iff devtools_alive
        endpoint = chrome.endpoint
        discovered_pid = chrome.pid

    # Validate profile/endpoint coherence: the Chrome at endpoint should be
    # running the requested profile's user-data-dir. Catches the case where
    # an unrelated Chrome stole the port we expected.
    if profile is not None:
        expected_dir = layout.profile_dir(profile).resolve()
        listeners = (
            find_listeners_on_endpoint(endpoint) if discovered_pid is None else [discovered_pid]
        )
        actual_dir = None
        actual_pid = None
        for pid in listeners:
            actual_dir = process_utils.find_chrome_user_data_dir(pid)
            actual_pid = pid
            if actual_dir is not None:
                break
        if actual_dir is not None and actual_dir != expected_dir:
            return error_response(
                f"Error: the Chrome at {endpoint} is running user-data-dir "
                f"{actual_dir} (pid {actual_pid}), not the requested profile "
                f"'{profile}' ({expected_dir}). Drop the explicit endpoint and call "
                f"attach_browser(profile='{profile}') to auto-discover the right port."
            )

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
    tabs = persistent_browser.enumerate_tabs(endpoint)
    if not tabs:
        return error_response(
            f"Error: E001 - Could not connect to Chrome at {endpoint}. "
            "Launch Chrome with --remote-debugging-port=9222"
        )

    # Auto-select tab if pattern provided
    selected_tab = None
    if tab_url:
        selected_tab = persistent_browser.select_tab_by_url(tabs, tab_url)
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

    return text_response("\n".join(lines))


def find_listeners_on_endpoint(endpoint: str) -> list[int]:
    """Return PIDs listening on the TCP port encoded in ``endpoint``.

    Args:
        endpoint: Chrome debug endpoint, e.g. ``http://127.0.0.1:9222``.

    Returns:
        Distinct PIDs, or empty when the port cannot be parsed or lsof is
        unavailable.
    """
    from urllib.parse import urlparse

    try:
        port = urlparse(endpoint).port
    except ValueError:
        return []
    if port is None:
        return []
    return process_utils.find_listeners_on_port(int(port))


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
    from .profile_catalog import describe_profile_runtime, list_profiles

    profiles = list_profiles()
    if not profiles:
        return text_response("No named profiles found.")

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
    return text_response(json.dumps(payload, indent=2, sort_keys=True))


def handle_delete_profile(args: Any) -> dict[str, Any]:
    """Handle the delete_profile tool.

    Args:
        args: Tool arguments with 'name'.

    Returns:
        JSON-RPC response dict.
    """
    from .profile_catalog import delete_profile

    name = args.get("name", "")
    if not name:
        return error_response("Error: profile name is required")

    if delete_profile(name):
        return text_response(f"Profile '{name}' deleted.")
    return error_response(f"Error: Profile '{name}' not found.")


def handle_browser_session_status(args: Any) -> dict[str, Any]:
    """Handle the browser_session_status tool.

    Args:
        args: Tool arguments (none required).

    Returns:
        JSON-RPC response dict with browser session diagnostics.
    """
    del args
    text = json.dumps(get_browser_session_status(), indent=2, sort_keys=True)
    return text_response(text)


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
        return text_response("Browser session override cleared; project preference will be used.")

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
        # When neither a profile nor an endpoint is given, auth now lands in
        # this project's own bucket (profile=None) rather than a shared global
        # "google-auth" named profile, so each project keeps its own login and
        # the default headless session reuses the same cookies.

    # Validate the resolved endpoint (whether from args or the project config)
    # before it is persisted and later dialed.
    if endpoint is not None:
        endpoint_error = validate_local_endpoint(endpoint)
        if endpoint_error:
            return error_response(f"Error: {endpoint_error}")

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
    if mode == "real":
        lines.append(
            "mode='real' drives your everyday Chrome profile (shared cookies/extensions/history), "
            "so there is a single dock icon that closes normally. If that Chrome is already open "
            "without --remote-debugging-port, quit it first so browser-tools can relaunch it with "
            "debugging enabled. Call close_browser to detach; it will not force-quit your browser."
        )
    return text_response("\n".join(lines))


def handle_close_browser(controller_ref: list[Any], fallback: Any, args: Any) -> dict[str, Any]:
    """Handle the close_browser tool: end the active browser session cleanly.

    Stops the background MCP daemon and either quits the Chrome the tool
    launched (private automation profile) or detaches from an external /
    real-profile Chrome, leaving it running. This is the supported way to end
    a session without hunting for and killing a process by hand.

    Args:
        controller_ref: Single-element list holding the active controller.
        fallback: Controller to fall back to when none has been swapped in.
        args: Tool arguments. ``reset_session`` (bool) also clears any explicit
            session override so the next call falls back to project preference.

    Returns:
        JSON-RPC response dict.
    """
    from .persistent_browser import close_active_session
    from .session_store import clear_session_override

    active = controller_ref[0] or fallback
    if active is None:
        return {
            "result": {"content": [{"type": "text", "text": "No active browser session to close."}]}
        }

    summary = close_active_session(active)
    controller_ref[0] = None
    if args.get("reset_session"):
        clear_session_override()

    if summary["quit_chrome"]:
        text = (
            f"Closed browser session: quit the tool-launched Chrome (pid {summary['pid']}) "
            "and stopped its background daemon."
        )
    elif summary.get("quit_failed"):
        text = (
            f"Stopped the background daemon, but Chrome (pid {summary['pid']}) is still "
            "running: the terminate signal did not take effect. Quit it manually with "
            f"`kill -9 {summary['pid']}`."
        )
    elif summary["detached"]:
        endpoint = summary["endpoint"] or "the attached browser"
        text = (
            f"Closed browser session: detached from {endpoint} and stopped the background daemon. "
            "The browser itself was left running (external or mode='real')."
        )
    else:
        text = "No running browser was found; cleared background session state."
    if args.get("reset_session"):
        text += " Session override cleared; project preference will be used next."
    return {"result": {"content": [{"type": "text", "text": text}]}}


# Session-level tools that don't need MCP daemon
SESSION_TOOLS = {
    "attach_browser",
    "list_profiles",
    "delete_profile",
    "use_browser_session",
    "browser_session_status",
    "close_browser",
}


def choose_live_profile_fallback(
    live: list[dict[str, Any]],
) -> PersistentChromeController | None:
    """Build a reuse controller when exactly one named profile is live.

    Only human-named profiles are auto-attach candidates. A hashed session key
    belongs to another project's default session — reusing it would hand this
    project a throwaway browser that project is still driving.

    Args:
        live: ``describe_profile_runtime`` descriptors for live profiles,
            excluding this project's own session.

    Returns:
        A controller configured to reuse the sole live named profile, or None.
    """
    named = [info for info in live if info.get("named")]
    if len(named) != 1:
        return None
    controller = PersistentChromeController(
        headless=False,
        isolated=False,
        channel="canary",
        profile=named[0]["profile"],
        force_persistent=True,
    )
    controller.mode = "full"
    return controller


def select_default_controller() -> tuple[PersistentChromeController, list[dict[str, Any]] | None]:
    """Pick the controller when no explicit session is configured.

    Prefers reusing a browser that is already open over launching another one,
    in priority order: this project's own live session, then a sole live named
    profile, then a fresh headless-isolated Chrome.

    Reusing this project's own session takes precedence because auto-attaching
    elsewhere would abandon a Chrome this project launched, leaving it to idle
    out as a second dock icon.

    Other projects' hashed sessions are reported but deliberately do not raise a
    conflict. Blocking here would stall every parallel agent whenever any other
    project had a browser open, and it would not reduce the browser count: the
    documented recovery, ``use_browser_session(mode='headless')``, resolves to
    this project's own session key and launches exactly the same Chrome. Several
    concurrent browsers is the correct state when several projects are active;
    the accumulation this guards against is browsers outliving their session,
    which ``reap_orphaned_sessions`` handles.

    Returns:
        ``(controller, conflict)``.

        - ``conflict`` is None whenever a browser could be picked unambiguously.
        - When several named profiles are live, returns the default
          headless-isolated controller and the live list as ``conflict`` so the
          caller can refuse non-session tools rather than launch yet another
          Chrome.
    """
    default = PersistentChromeController(
        headless=True,
        channel="canary",
        force_persistent=True,
    )
    live = find_live_profiles()
    if any(info.get("profile") == default.session_key for info in live):
        # This project's own session is already running — reuse it.
        return default, None

    others = [info for info in live if info.get("profile") != default.session_key]
    fallback = choose_live_profile_fallback(others)
    if fallback is not None:
        return fallback, None

    conflict = others if sum(1 for info in others if info.get("named")) > 1 else None
    return default, conflict


@dataclass
class SessionResolution:
    """The controller this project should use, and why.

    Returned by :func:`resolve_session_controller` and consumed by both the
    session bootstrap (``create_session``) and status reporting
    (:func:`session_store.get_browser_session_status`) so the two report the
    same choice instead of re-deriving the priority order independently.
    """

    controller: PersistentChromeController
    source: str
    conflict: list[dict[str, Any]] | None


def resolve_session_controller() -> SessionResolution:
    """Resolve which controller this project should use, in priority order.

    Single owner of the Active-Session resolution priority:

    1. explicit session override
    2. project preference (``.browser-tools.json``)
    3. recent external attach (live and within TTL)
    4. default selection - this project's own live session, else a sole live
       named profile, else a fresh headless Chrome - with a ``conflict``
       descriptor when several named profiles are live and none is picked.

    Returns:
        The chosen controller, the source label, and any live-profile conflict.
    """
    override = create_session_override_controller()
    if override is not None:
        return SessionResolution(override, "override", None)

    preferred = create_project_preferred_controller()
    if preferred is not None:
        return SessionResolution(preferred, "project", None)

    attached = load_active_attach_controller()
    if attached is not None:
        return SessionResolution(attached, "active_attach", None)

    default, conflict = select_default_controller()
    if conflict is not None:
        return SessionResolution(default, "default_headless", conflict)
    if not default.isolated and default.profile:
        return SessionResolution(default, "live_profile_fallback", None)
    return SessionResolution(default, "default_headless", None)


# ---------------------------------------------------------------------------
# Session lifecycle: single-tab navigation, close_browser, auth promotion
# ---------------------------------------------------------------------------


