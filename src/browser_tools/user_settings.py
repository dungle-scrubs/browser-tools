"""Persistent user settings, kept across launches and read by live supervisors.

One JSON object in ``$XDG_CONFIG_HOME/browser-tools/settings.json``
(``~/.config/browser-tools/settings.json`` when ``XDG_CONFIG_HOME`` is unset).
A missing, unreadable, or malformed file means every setting has its default.

Settings:

- ``window_border`` (``"on"`` | ``"off"``, default ``"on"``) -- whether the
  window marker draws its border and corner badge inside the page. The border
  sits on top of the page's outer edge and the badge on its top-left corner,
  so a person who needs to see that UI turns it off. The supervisor of every
  running instance re-reads this setting every second and adds or removes the
  border on its open tabs to match, so a change applies to running browsers
  as well as later launches. The tab-title prefix is not affected: it lives in
  the tab strip and covers nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

WINDOW_BORDER_KEY = "window_border"
ON = "on"
OFF = "off"


def settings_path() -> Path:
    """The settings file path under the XDG config directory."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "browser-tools" / "settings.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def window_border_enabled(path: Path | None = None) -> bool:
    """Whether the in-page border and badge are on (default: on)."""
    return _load(path or settings_path()).get(WINDOW_BORDER_KEY) != OFF


def set_window_border(enabled: bool, path: Path | None = None) -> Path:
    """Store the border setting, keeping any other settings. Returns the path.

    The write is atomic (temp file + rename) so a supervisor reading the file
    every second never sees a half-written object.
    """
    path = path or settings_path()
    data = _load(path)
    data[WINDOW_BORDER_KEY] = ON if enabled else OFF
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
