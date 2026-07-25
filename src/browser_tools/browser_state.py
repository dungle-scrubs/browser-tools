"""Persisted browser state dataclasses for browser-tools.

Extracted from persistent_browser.py to keep the module under 800 lines.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path  # noqa: TC003
from typing import Any

# Canonical session-mode vocabulary, defined once so the wrapper, the override
# handler, and the controller factory cannot drift out of sync.
#   - HEADED_AUTH_MODES: launch/reuse a headed, persistent, login-bearing session.
#   - HEADLESS_AUTH_MODES: persistent profile but headless (may be challenged).
# "headed", "headed-auth", "auth", and "auth-headed" are exact synonyms.
HEADED_AUTH_MODES = frozenset({"headed", "headed-auth", "auth", "auth-headed"})
HEADLESS_AUTH_MODES = frozenset({"headless-auth"})
AUTH_MODES = HEADED_AUTH_MODES | HEADLESS_AUTH_MODES


def normalize_mode(mode: str) -> str:
    """Normalize a session-mode string to its canonical spelling.

    Args:
        mode: Raw mode value (any case, underscores or hyphens).

    Returns:
        Lowercased, hyphenated mode string.
    """
    return mode.lower().replace("_", "-")


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from disk, returning None on any failure.

    Args:
        path: JSON file to read.

    Returns:
        The parsed mapping, or None when the file is missing, unreadable,
        malformed, or not a JSON object.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_json_dict(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON object with owner-only permissions.

    State files can hold session identifiers and socket paths that control a
    logged-in browser, so they are written 0o600. The write goes to a
    temp file and is renamed into place so a concurrent reader never observes
    a truncated file.

    Args:
        path: Destination JSON file.
        data: JSON-serializable mapping to persist.

    Returns:
        None.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class _PersistedConfig:
    """Mixin providing atomic, owner-only JSON load/save for dataclasses.

    Subclasses must be dataclasses. ``from_path`` tolerates unknown keys
    (forward compatibility) and returns None on any read error.
    """

    @classmethod
    def from_path(cls, path: Path):
        """Load an instance from disk, or None when missing/invalid.

        Args:
            path: JSON file to read.

        Returns:
            A populated instance, or None.
        """
        data = _read_json_dict(path)
        if data is None:
            return None
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]):
        """Build an instance from a mapping, ignoring unknown keys.

        Args:
            data: Parsed JSON mapping.

        Returns:
            A populated instance, or None when required fields are missing.
        """
        allowed = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        try:
            return cls(**{key: value for key, value in data.items() if key in allowed})
        except TypeError:
            return None

    def save(self, path: Path) -> None:
        """Persist this instance to disk atomically with mode 0o600.

        Args:
            path: JSON file to write.

        Returns:
            None.
        """
        _write_json_dict(path, asdict(self))  # type: ignore[call-overload]


@dataclass
class BrowserState(_PersistedConfig):
    """Persisted browser state shared across wrapper invocations."""

    browser_url: str
    selected_page_id: int | None = None
    selected_page_url: str | None = None
    pid: int | None = None
    user_data_dir: str | None = None
    headless: bool = False
    isolated: bool = True
    channel: str = "canary"
    viewport: str | None = None
    last_used_at: float = 0.0
    daemon_pid: int | None = None
    daemon_socket: str | None = None
    # True only when this tool launched ``pid`` itself. Recorded rather than
    # inferred, because inferring ownership from the profile directory alone
    # cannot tell a tool-launched Chrome from one a user started on the same
    # directory and attached to. Defaults False so a state file written before
    # this field existed is treated as not-ours and never force-quit.
    chrome_owned: bool = False
    # ``ps`` start-time of ``pid``, pinning process identity across the window
    # between deciding to quit a browser and actually signalling it.
    chrome_started_at: str | None = None


@dataclass
class ActiveAttachConfig(_PersistedConfig):
    """Persisted configuration for the currently attached external Chrome.

    Stored separately from BrowserState so future tool-proxy invocations can
    recreate the correct controller after attach_browser has exited.
    """

    browser_url: str
    profile: str | None = None
    # Access mode for the attached browser (e.g. "full", "read-only"); distinct
    # from ProjectBrowserConfig.mode, which selects a session type.
    mode: str = "full"
    stealth: bool = False
    saved_at: float = 0.0


@dataclass
class ProjectBrowserConfig(_PersistedConfig):
    """Preferred browser session configuration loaded from a project file."""

    # Session type selector (e.g. "headless", "headed-auth", "headless-auth");
    # distinct from ActiveAttachConfig.mode, which is an access mode.
    mode: str = "headless"
    profile: str | None = None
    endpoint: str | None = None
    browser_url: str | None = None
    headless: bool | None = None
    isolated: bool | None = None
    channel: str = "canary"
    viewport: str | None = None
    stealth: bool = False
    saved_at: float = 0.0

    @classmethod
    def from_path(cls, path: Path) -> ProjectBrowserConfig | None:
        """Load a project browser preference file.

        Unwraps a ``preferred_session`` / ``preferredSession`` envelope when
        present so both flat and nested config shapes are accepted.

        Args:
            path: JSON project preference path.

        Returns:
            Parsed project browser config, or None when missing/invalid.
        """
        data = _read_json_dict(path)
        if data is None:
            return None
        if isinstance(data.get("preferred_session"), dict):
            data = data["preferred_session"]
        if isinstance(data.get("preferredSession"), dict):
            data = data["preferredSession"]
        return cls._from_dict(data)


__all__ = [
    "AUTH_MODES",
    "HEADED_AUTH_MODES",
    "HEADLESS_AUTH_MODES",
    "ActiveAttachConfig",
    "BrowserState",
    "ProjectBrowserConfig",
    "normalize_mode",
]
