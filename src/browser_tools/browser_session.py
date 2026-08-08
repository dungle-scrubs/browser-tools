"""Tool-proxy session adapter: routes MCP tool calls to the active browser.

Split out of the CLI (browser_tools_session). Owns session-tool dispatch
(attach/use_browser_session/close_browser), Camoufox routing, single-tab
enforcement, headless->headed auth-wall promotion, and live-profile conflict
resolution. The CLI stays in browser_tools_session.
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass
from typing import Any

from . import persistent_browser, process_utils
from . import session_layout as layout
from .automation_backend import CAMOUFOX_TOOL_MAP, CamoufoxBackend, ChromeBackend
from .browser_state import (
    AUTH_MODES,
    HEADED_AUTH_MODES,
    HEADLESS_AUTH_MODES,
    ProjectBrowserConfig,
    normalize_mode,
)
from .chrome_utils import BrowserToolsError
from .live_chrome import resolve_live_chrome
from .mcp_response import error_response, extract_text_items, text_response
from .persistent_browser import PersistentChromeController, format_dead_port_error
from .process_utils import validate_local_endpoint
from .profile_catalog import find_live_profiles
from .session_reaper import reap_orphaned_sessions
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
from .tool_registry import NAVIGATION_TOOLS, SINGLE_TAB_TOOLS


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


def _extract_page_ids(list_response: dict[str, Any]) -> list[int]:
    """Extract page ids (in listed order) from a list_pages response.

    Args:
        list_response: Raw JSON-RPC response from list_pages.

    Returns:
        Ordered list of integer page ids.
    """
    from .page_selection import PAGE_LINE_PATTERN

    ids: list[int] = []
    for text in extract_text_items(list_response):
        for match in PAGE_LINE_PATTERN.finditer(text):
            try:
                ids.append(int(match.group(1)))
            except ValueError:
                continue
    return ids


def _handle_new_page_single_tab(controller: PersistentChromeController, url: Any) -> dict[str, Any]:
    """Open ``url`` in the single active tab instead of stacking a new one.

    Navigates the existing first tab to the URL, then closes every other tab
    so the browser holds exactly one page. This is the single-tab model that
    stops the accumulation the agent otherwise causes by calling new_page.

    Args:
        controller: Active persistent controller.
        url: URL to load.

    Returns:
        The navigate_page response for the reused tab (or a fresh list_pages
        response if extra tabs were closed).
    """
    ids = _extract_page_ids(controller.invoke_tool("list_pages", {}))
    if not ids:
        # No tab at all (launch normally opens about:blank); create one.
        return controller.invoke_tool("new_page", {"url": url})

    controller.invoke_tool("select_page", {"pageId": ids[0]})
    response = controller.invoke_tool("navigate_page", {"type": "url", "url": url})

    # Close any remaining tabs. Re-list each iteration: closing a page can
    # renumber the rest, so a stale id list would miss or mis-target one.
    closed_any = False
    for _ in range(20):  # hard cap to avoid an infinite loop on misbehavior
        current = _extract_page_ids(controller.invoke_tool("list_pages", {}))
        extras = current[1:] if len(current) > 1 else []
        if not extras:
            break
        try:
            controller.invoke_tool("close_page", {"pageId": extras[0]})
            closed_any = True
        except BrowserToolsError:
            break
    if closed_any:
        response = controller.invoke_tool("list_pages", {})
    return response


def _response_signals_auth_wall(response: dict[str, Any]) -> bool:
    """Return whether a navigation response carries an auth-wall interstitial.

    The daemon appends an interstitial summary to navigation responses; the
    auth_wall detector fires on login forms and sign-in page titles.

    Args:
        response: Raw JSON-RPC response from a navigation tool.

    Returns:
        True when an auth_wall signal is present in the response text.
    """
    return any("auth_wall" in text for text in extract_text_items(response))


def _maybe_promote_on_auth_wall(
    controller_ref: list[Any],
    controller: PersistentChromeController,
    response: dict[str, Any],
    url: Any,
) -> dict[str, Any]:
    """Promote a headless session to headed when a navigation hits auth.

    Headless Chrome cannot complete an OAuth/login handshake, so when the
    interstitial detector reports an auth wall on a headless session the
    headless Chrome is torn down and a headed one is launched on the same
    profile dir (cookies survive on disk). The caller is told to finish
    sign-in; future headless calls then reuse the authenticated profile.

    Args:
        controller_ref: Single-element list holding the active controller.
        controller: Controller used for the navigation.
        response: Navigation response to inspect.
        url: URL to re-navigate in the headed window.

    Returns:
        The original response, or a promotion notice when promoted.
    """
    if not getattr(controller, "headless", False):
        return response
    if not _response_signals_auth_wall(response):
        return response
    headed = _promote_headless_to_headed(controller, url if isinstance(url, str) else None)
    if headed is None:
        return response
    controller_ref[0] = headed
    target = url if isinstance(url, str) else "the page"
    notice = (
        "Auth required — switched from headless to a headed window on the same "
        f"profile at {target}. Complete sign-in there, then re-run the action. "
        "Login persists on disk, so future headless sessions reuse it without "
        "re-auth."
    )
    return text_response(notice)


def _format_live_profile_conflict_error(live: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a tool-proxy error response describing multiple live profiles.

    Args:
        live: ``describe_profile_runtime`` descriptors for live profiles.

    Returns:
        MCP-style error response telling the agent how to disambiguate.
    """
    lines = [
        f"Multiple browsers are live ({len(live)}). Refusing to launch a new headless",
        "Chrome that would ignore them. Pick one explicitly:",
        "",
    ]
    for info in live:
        name = info.get("profile", "?")
        endpoint = info.get("endpoint") or "?"
        url = info.get("current_url") or "(no active page)"
        tabs = info.get("tab_count", 0)
        # Hashed session keys have no addressable name: another project's
        # default session is reachable by endpoint only.
        suffix = "" if info.get("named") else "  [another project's session]"
        lines.append(f"  - {name}: {endpoint} ({tabs} tab(s)) — {url}{suffix}")
    lines.extend(
        [
            "",
            "Recover with one of:",
            "  use_browser_session(mode='headed-auth', profile='<name>')",
            "  attach_browser(profile='<name>')",
            "  attach_browser(endpoint='<endpoint>')  for a session with no name",
            "Or call use_browser_session(mode='headless') to opt into a fresh isolated session.",
        ]
    )
    return error_response("\n".join(lines))


def _promote_headless_to_headed(
    controller: PersistentChromeController, url: str | None
) -> PersistentChromeController | None:
    """Tear down the headless Chrome and relaunch headed on the same profile.

    Args:
        controller: Headless controller to replace.
        url: Optional URL to navigate in the headed window.

    Returns:
        A new headed controller, or None if relaunch failed.
    """
    # Quit the headless Chrome + daemon but keep the user-data-dir (cookies).
    controller.close_owned_browser()
    headed = PersistentChromeController(
        headless=False,
        channel=controller.channel,
        profile=controller.profile,
        browser_url=None,
        force_persistent=True,
    )
    headed.mode = controller.mode or "full"
    try:
        headed.ensure_browser_state()
    except BrowserToolsError:
        return None
    if url:
        with contextlib.suppress(BrowserToolsError):
            headed.invoke_tool("navigate_page", {"type": "url", "url": url})
    return headed


@dataclass
class SessionDispatchContext:
    """Collaborators for session-adapter tool dispatch.

    Holds the mutable controller / Camoufox / conflict state that
    :func:`create_session` populates and :func:`dispatch_session_tool` reads.
    Built once per tool-proxy session. Passing it explicitly (rather than
    closing over it) gives dispatch a test seam: tests build a context with
    fakes and call ``dispatch_session_tool`` directly, the session-adapter
    sibling of the Daemon's ``DispatchContext``.
    """

    controller_ref: list[Any]
    camoufox_ref: list[Any]
    live_profile_conflict: list[Any]


def dispatch_session_tool(
    ctx: SessionDispatchContext,
    controller: PersistentChromeController,
    tool: str,
    args: Any,
) -> dict[str, Any]:
    """Route one tool call to the active backend, applying cross-cutting policy.

    The session-adapter counterpart of the Daemon's ``dispatch_tool``. Routing
    order is load-bearing (see ``automation_backend``): Camoufox automation
    tools resolve before the session-management tools and the live-profile-
    conflict gate, while Chrome's navigation hooks run after them. Within the
    Chrome path the two cross-cutting policies are registry-driven instead of
    inline branches:

    - ``SINGLE_TAB_TOOLS`` - ``new_page`` reuses the one active tab.
    - ``NAVIGATION_TOOLS`` - a URL navigation may trigger headless-to-headed
      auth-wall promotion.

    Args:
        ctx: Mutable dispatch collaborators (controller / Camoufox / conflict).
        controller: Controller in force when the tool-proxy adapter built this
            session; superseded by ``ctx.controller_ref`` when an
            ``attach_browser`` / ``use_browser_session`` call swapped it.
        tool: browser-tools tool name.
        args: Tool arguments.

    Returns:
        MCP JSON-RPC response dict.
    """
    # --- Camoufox-exclusive lifecycle tools ---
    if tool == "launch_camoufox":
        return _handle_launch_camoufox(ctx.camoufox_ref, args)
    if tool == "close_camoufox":
        return _handle_close_camoufox(ctx.camoufox_ref)
    if tool in ("wait_for_human", "get_cookies"):
        cfox = ctx.camoufox_ref[0]
        if cfox is None:
            return error_response(
                f"Error: {tool} requires an active Camoufox session. "
                "Call launch_camoufox first."
            )
        return CamoufoxBackend(cfox).invoke(tool, args)

    # --- Standard tools routed through Camoufox when active ---
    cfox = ctx.camoufox_ref[0]
    if cfox is not None and tool in CAMOUFOX_TOOL_MAP:
        return CamoufoxBackend(cfox).invoke(tool, args)

    # --- Session-management tools (swap the controller / override) ---
    if tool == "attach_browser":
        return handle_attach_browser(ctx.controller_ref, args)
    if tool == "use_browser_session":
        return handle_use_browser_session(ctx.controller_ref, args)
    if tool == "browser_session_status":
        return handle_browser_session_status(args)
    if tool == "close_browser":
        return handle_close_browser(ctx.controller_ref, controller, args)
    if tool == "list_profiles":
        return handle_list_profiles(args)
    if tool == "delete_profile":
        return handle_delete_profile(args)

    # If multiple live profiles were detected at session creation and the agent
    # hasn't picked one yet (attach_browser / use_browser_session would have
    # cleared the conflict by swapping controller_ref or saving an override),
    # refuse non-session tools rather than silently spawning a new headless
    # Chrome.
    if ctx.live_profile_conflict[0] is not None and ctx.controller_ref[0] is controller:
        return _format_live_profile_conflict_error(ctx.live_profile_conflict[0])

    # Use latest controller (may have been swapped by attach_browser).
    active = ctx.controller_ref[0] or controller
    chrome = ChromeBackend(active)
    url = args.get("url")

    # Single active tab: new_page reuses the one tab instead of stacking.
    if tool in SINGLE_TAB_TOOLS:
        response = _handle_new_page_single_tab(active, url)
    else:
        response = chrome.invoke(tool, args)

    # Headless -> headed auto-promotion when a URL navigation hits an auth wall.
    # new_page always navigates to a URL; navigate_page only when type=url (the
    # default). back/forward/reload re-auth is not a fresh login-wall event.
    if tool in NAVIGATION_TOOLS and (tool in SINGLE_TAB_TOOLS or args.get("type", "url") == "url"):
        response = _maybe_promote_on_auth_wall(ctx.controller_ref, active, response, url)

    return response


def create_tool_proxy_handlers():
    """Create stateful handlers for a tool-proxy protocol adapter.

    Returns a ``(create_session, call_tool)`` pair sharing one
    :class:`SessionDispatchContext`. ``call_tool`` is a thin closure over the
    context that delegates to :func:`dispatch_session_tool`, so the routing
    policy lives in one testable place rather than in the closure body.

    The standalone package intentionally does not import py_utils. The
    tool-proxy app adapter owns protocol I/O and calls these handlers.
    """
    ctx = SessionDispatchContext(
        controller_ref=[None],
        camoufox_ref=[None],
        live_profile_conflict=[None],
    )

    def create_session() -> PersistentChromeController:
        # Quit automation Chromes left behind by daemons that died without
        # running their own teardown, before deciding what is live.
        for orphan in reap_orphaned_sessions():
            print(
                f"[browser-tools] Reaped orphaned Chrome (pid {orphan['pid']}, "
                f"session {orphan['session_key']})",
                file=sys.stderr,
            )

        # One owner of the resolution priority (see resolve_session_controller),
        # so the bootstrap and browser_session_status cannot drift apart.
        resolution = resolve_session_controller()
        ctx.controller_ref[0] = resolution.controller
        if resolution.conflict is not None:
            ctx.live_profile_conflict[0] = resolution.conflict
        elif resolution.source == "live_profile_fallback":
            print(
                f"[browser-tools] Auto-attached to sole live profile "
                f"'{resolution.controller.profile}'",
                file=sys.stderr,
            )
        return resolution.controller

    def call_tool(controller: PersistentChromeController, tool: str, args: Any) -> dict[str, Any]:
        return dispatch_session_tool(ctx, controller, tool, args)

    return create_session, call_tool


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
        return text_response("Camoufox is already running. Call close_camoufox first to restart.")

    session = CamoufoxSession()
    result = session.call_tool("launch_browser", args)

    if "error" in result:
        return error_response(f"Error launching Camoufox: {result['error']}")

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
    return text_response("\n".join(lines))


def _handle_close_camoufox(camoufox_ref: list[Any]) -> dict[str, Any]:
    """Close the Camoufox session and switch back to Chrome.

    Args:
        camoufox_ref: Single-element list holding the active CamoufoxSession.

    Returns:
        MCP-style response.
    """
    session = camoufox_ref[0]
    if session is None:
        return text_response("No Camoufox session is active.")

    session.call_tool("close_browser", {})
    camoufox_ref[0] = None

    return text_response("Camoufox closed. Standard tools now route through Chrome again.")
