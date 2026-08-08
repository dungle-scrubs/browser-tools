"""Per-project browser session configuration and controller selection.

Owns the three on-disk "which controller should this project use" records -
the explicit session override, the project preference file
(``.browser-tools.json``), and the active external attach - plus the factories
that turn each into a ``PersistentChromeController``. It does not launch
browsers or manage Chrome processes; the controller lives in
``persistent_browser`` and the on-disk layout (``CACHE_DIR``) in
``session_layout``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import session_layout as layout
from .browser_state import (
    HEADLESS_AUTH_MODES,
    ActiveAttachConfig,
    ProjectBrowserConfig,
    normalize_mode,
)
from .persistent_browser import PersistentChromeController
from .process_utils import is_devtools_available
from .project_identity import get_project_dir, get_project_id, resolve_project_root

ACTIVE_ATTACH_TTL_SECONDS = 12 * 60 * 60
PROJECT_CONFIG_FILENAMES = (
    ".browser-tools.json",
    str(Path(".tool-proxy") / "browser-tools.json"),
)


def get_project_cwd() -> Path:
    """Return the project working directory used for browser preferences.

    Reads the harness-provided project directory (new canonical
    ``TOOL_PROXY_PROJECT_DIR`` name first, legacy ``CLAUDE_CWD`` as fallback)
    so browser-tools is not coupled to one harness's env var name.

    Returns:
        Absolute project working directory path.
    """
    return get_project_dir()


def _project_key_suffix() -> str:
    """Return the short hash identifying the current project's config files.

    Keyed on the resolved project root (git root, else cwd) rather than the
    raw working directory so config/state files stay stable as the agent
    moves between subdirectories of one repository.

    Returns:
        16-char hex suffix derived from CLAUDE_PROJECT_ID and the project root.
    """
    raw_key = f"{get_project_id()}|{resolve_project_root()}"
    return hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]


def get_active_attach_config_path() -> Path:
    """Return the per-project file used to remember the active Chrome attach.

    Returns:
        Path to the active attach config JSON file.
    """
    return layout.CACHE_DIR / f"active_attach_{_project_key_suffix()}.json"


def find_project_browser_config_path() -> Path | None:
    """Find the nearest browser-tools preference file for the current project.

    Returns:
        Matching config path, or None when no config exists.
    """
    cwd = get_project_cwd()
    for directory in (cwd, *cwd.parents):
        for filename in PROJECT_CONFIG_FILENAMES:
            candidate = directory / filename
            if candidate.exists():
                return candidate
    return None


def load_project_browser_config() -> ProjectBrowserConfig | None:
    """Load the current project's preferred browser-tools session config.

    Returns:
        Project browser config, or None when no usable config exists.
    """
    path = find_project_browser_config_path()
    if path is None:
        return None
    return ProjectBrowserConfig.from_path(path)


def create_controller_from_browser_config(
    config: ProjectBrowserConfig,
    *,
    source: str,
) -> PersistentChromeController:
    """Create a persistent controller from a browser session config.

    Args:
        config: Browser session configuration.
        source: Source label for diagnostics.

    Returns:
        Configured browser controller.
    """
    del source  # Reserved for future response diagnostics.
    mode = normalize_mode(config.mode)
    browser_url = config.endpoint or config.browser_url

    # Per-mode defaults for presentation (headless) and persistence (isolated),
    # each overridable by an explicit config field. Only the plain "headless"
    # mode is isolated; every auth mode keeps a persistent profile.
    if mode == "headless":
        default_headless, default_isolated = True, True
    elif mode in HEADLESS_AUTH_MODES:
        default_headless, default_isolated = True, False
    else:  # headed / headed-auth / auth / auth-headed / unknown
        default_headless, default_isolated = False, False

    headless = config.headless if config.headless is not None else default_headless
    isolated = config.isolated if config.isolated is not None else default_isolated

    # A named profile always persists login state, so it can never be isolated
    # (the PersistentChromeController constructor rejects that combination, E005).
    if config.profile:
        isolated = False

    # mode='real' drives the user's everyday Chrome profile. It never uses a
    # private/isolated profile, but the agent may still choose headed/headless.
    if mode == "real":
        controller = PersistentChromeController(
            headless=headless if config.headless is not None else False,
            isolated=False,
            viewport=config.viewport,
            channel=config.channel,
            stealth=config.stealth,
            system_profile=True,
            force_persistent=True,
        )
        controller.mode = "full"
        return controller

    controller = PersistentChromeController(
        headless=headless,
        isolated=isolated,
        viewport=config.viewport,
        channel=config.channel,
        browser_url=browser_url,
        profile=config.profile,
        stealth=config.stealth,
        force_persistent=True,
    )
    controller.mode = "full"
    return controller


def create_project_preferred_controller() -> PersistentChromeController | None:
    """Create a controller from the project's preferred browser config.

    Returns:
        Configured controller, or None when no project preference is set.
    """
    config = load_project_browser_config()
    if config is None:
        return None
    return create_controller_from_browser_config(config, source="project")


def get_session_override_path() -> Path:
    """Return the per-project browser session override file path.

    Returns:
        Path to the browser session override JSON file.
    """
    return layout.CACHE_DIR / f"browser_session_{_project_key_suffix()}.json"


def save_session_override(config: ProjectBrowserConfig) -> None:
    """Persist a browser session override for the current project.

    Args:
        config: Browser session override to save.

    Returns:
        None.
    """
    config.saved_at = time.time()
    path = get_session_override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True))


def clear_session_override() -> None:
    """Remove the browser session override for the current project.

    Returns:
        None.
    """
    get_session_override_path().unlink(missing_ok=True)


def load_session_override() -> ProjectBrowserConfig | None:
    """Load the current project's explicit browser session override.

    Returns:
        Browser session override, or None when not set.
    """
    return ProjectBrowserConfig.from_path(get_session_override_path())


def create_session_override_controller() -> PersistentChromeController | None:
    """Create a controller from the explicit browser session override.

    Returns:
        Configured controller, or None when no override is set.
    """
    config = load_session_override()
    if config is None:
        return None
    return create_controller_from_browser_config(config, source="override")


def get_browser_session_status() -> dict[str, Any]:
    """Return project browser session diagnostics.

    Returns:
        Browser session status for override, project preference, and active
        attach, plus every browser-tools Chrome currently live on this machine
        so an unexpected extra instance is visible rather than inferred.
    """
    from .profile_catalog import find_live_profiles

    override = load_session_override()
    project_path = find_project_browser_config_path()
    project = load_project_browser_config()
    active = ActiveAttachConfig.from_path(get_active_attach_config_path())
    active_live = active is not None and is_devtools_available(active.browser_url)
    selected_source = (
        "override"
        if override
        else "project"
        if project
        else "active_attach"
        if active_live
        else "default_headless"
    )
    return {
        "selected_source": selected_source,
        "override": asdict(override) if override else None,
        "project_config_path": str(project_path) if project_path else None,
        "project_preference": asdict(project) if project else None,
        "active_attach": asdict(active) if active else None,
        "active_attach_live": active_live,
        "default": {"mode": "headless", "headless": True, "isolated": True, "channel": "canary"},
        "live_browsers": [
            {
                "profile": info["profile"],
                "named": info["named"],
                "pid": info["pid"],
                "endpoint": info["endpoint"],
                "tab_count": info["tab_count"],
                "current_url": info["current_url"],
            }
            for info in find_live_profiles()
        ],
    }


def save_active_attach_config(
    browser_url: str,
    *,
    profile: str | None = None,
    mode: str = "full",
    stealth: bool = False,
) -> None:
    """Persist the current external Chrome attachment for future tool calls.

    Args:
        browser_url: Remote debugging endpoint.
        profile: Optional named browser profile.
        mode: Access mode.
        stealth: Whether stealth patches are enabled.

    Returns:
        None.
    """
    ActiveAttachConfig(
        browser_url=browser_url,
        profile=profile,
        mode=mode,
        stealth=stealth,
        saved_at=time.time(),
    ).save(get_active_attach_config_path())


def clear_active_attach_config() -> None:
    """Remove the saved external Chrome attachment for this project.

    Returns:
        None.
    """
    get_active_attach_config_path().unlink(missing_ok=True)


def load_active_attach_controller() -> PersistentChromeController | None:
    """Recreate the attached-browser controller from saved project state.

    Returns:
        Configured controller when a recent live attach exists, otherwise None.
    """
    config = ActiveAttachConfig.from_path(get_active_attach_config_path())
    if config is None:
        return None
    if time.time() - config.saved_at > ACTIVE_ATTACH_TTL_SECONDS:
        clear_active_attach_config()
        return None
    if not is_devtools_available(config.browser_url):
        # Transient unreachability (Chrome restarting, port not yet up) must not
        # permanently drop the attachment to a fresh logged-out default. Keep
        # the saved config so the next call can reattach once Chrome is back;
        # only the TTL above clears it.
        return None

    controller = PersistentChromeController(
        isolated=False,
        browser_url=config.browser_url,
        profile=config.profile,
        stealth=config.stealth,
        force_persistent=True,
    )
    controller.mode = config.mode
    return controller
