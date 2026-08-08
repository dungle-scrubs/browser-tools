"""Harness-agnostic project identity for browser-tools.

Resolves the current project to a stable root directory so session keying:

- does not fragment when the agent's working directory drifts within a
  single repository (subdir drift used to hash to a different bucket and
  spawn a second Chrome, losing auth), and
- does not depend on a Claude-specific env var name. tool-proxy sets the
  caller's project directory for every client (pi, Codex, Claude Code,
  anything); the legacy ``CLAUDE_CWD`` / ``CLAUDE_PROJECT_ID`` names are
  accepted as fallbacks, with new canonical ``TOOL_PROXY_*`` names
  preferred so the coupling to one harness's name is gone.

Project root resolution walks up from the working directory to the nearest
``.git`` marker (directory or file, covering worktrees and submodules) and
falls back to the directory itself when no checkout is found. A whole
repository is treated as one project, so every call from anywhere inside it
resolves to the same browser instance.
"""

from __future__ import annotations

import os
from pathlib import Path

# Preferred (harness-agnostic) names first; legacy Claude names kept as
# fallback so this keeps working unchanged until tool-proxy is renamed.
_PROJECT_DIR_ENVS: tuple[str, ...] = ("TOOL_PROXY_PROJECT_DIR", "CLAUDE_CWD")
_PROJECT_ID_ENVS: tuple[str, ...] = ("TOOL_PROXY_PROJECT_ID", "CLAUDE_PROJECT_ID")


def get_project_dir() -> Path:
    """Return the caller's project working directory.

    Reads the harness-provided env var (new canonical name first, legacy
    Claude name as fallback) and falls back to the current working
    directory when no override is present.

    Returns:
        Resolved project directory path.
    """
    for name in _PROJECT_DIR_ENVS:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve()
    return Path.cwd().resolve()


def get_project_id() -> str:
    """Return the caller's project id, or an empty string.

    Returns:
        Project id string (may be empty when unset).
    """
    for name in _PROJECT_ID_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def resolve_project_root(start: Path | None = None) -> Path:
    """Walk up to the nearest VCS root, else return the start directory.

    A repository is treated as one project: every directory inside it
    resolves to the repository root, so session keying is stable regardless
    of which subdirectory the agent happens to be in.

    Args:
        start: Starting directory (defaults to :func:`get_project_dir`).
            A file path is reduced to its parent directory.

    Returns:
        The repository root when a ``.git`` marker is found above ``start``,
        otherwise ``start`` itself resolved.
    """
    directory = (start or get_project_dir()).resolve()
    if directory.is_file():
        directory = directory.parent
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory
