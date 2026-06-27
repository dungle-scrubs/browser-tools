"""Persisted browser state dataclasses for browser-tools.

Extracted from persistent_browser.py to keep the module under 800 lines.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path  # noqa: TC003


@dataclass
class BrowserState:
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

    @classmethod
    def from_path(cls, path: Path) -> BrowserState | None:
        """Load browser state from disk if it exists and is valid.

        Args:
            path: JSON file containing persisted browser state.

        Returns:
            BrowserState when the file exists and parses successfully, otherwise None.
        """
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return cls(**data)
        except TypeError:
            return None

    def save(self, path: Path) -> None:
        """Persist browser state to disk.

        Args:
            path: JSON file to write.

        Returns:
            None.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))


@dataclass
class ActiveAttachConfig:
    """Persisted configuration for the currently attached external Chrome.

    Stored separately from BrowserState so future tool-proxy invocations can
    recreate the correct controller after attach_browser has exited.
    """

    browser_url: str
    profile: str | None = None
    mode: str = "full"
    stealth: bool = False
    saved_at: float = 0.0

    @classmethod
    def from_path(cls, path: Path) -> ActiveAttachConfig | None:
        """Load config from disk.

        Args:
            path: JSON file containing the saved attach config.

        Returns:
            Parsed config, or None when the file is missing/invalid.
        """
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return cls(**data)
        except TypeError:
            return None

    def save(self, path: Path) -> None:
        """Persist config to disk.

        Args:
            path: JSON file to write.

        Returns:
            None.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))


@dataclass
class ProjectBrowserConfig:
    """Preferred browser session configuration loaded from a project file."""

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

        Args:
            path: JSON project preference path.

        Returns:
            Parsed project browser config, or None when missing/invalid.
        """
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("preferred_session"), dict):
            data = data["preferred_session"]
        if isinstance(data.get("preferredSession"), dict):
            data = data["preferredSession"]
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


__all__ = [
    "ActiveAttachConfig",
    "BrowserState",
    "ProjectBrowserConfig",
]
