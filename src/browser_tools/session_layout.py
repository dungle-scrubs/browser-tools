"""On-disk layout for browser-tools sessions.

Single owner of *where* browser-tools writes: the cache directory, the named
profiles directory, and the per-session runtime files (state JSON, daemon
Unix socket, daemon PID file, spawn lock). Also owns session-key derivation
and the file-level cleanup/enumeration helpers. Every other module reads
paths through this one, so a path convention changes in one place and the
named-profile catalog, the session store, the controller, and the reaper all
follow.

CACHE_DIR is a module global read at call time by every function here, so a
test that monkeypatches ``session_layout.CACHE_DIR`` moves the whole layout
at once - no per-module dynamic reads, no re-export layer. This is the seam
the catalog and session store previously reached into ``persistent_browser``
for.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .project_identity import get_project_id, resolve_project_root

CACHE_DIR = Path.home() / ".cache" / "tool-proxy" / "browser-tools"
INITIAL_PAGE_URL = "about:blank"


# --------------------------------------------------------------------------- #
# Directory / file path builders
# --------------------------------------------------------------------------- #


def profiles_dir() -> Path:
    """Return the directory holding every profile (named and per-project)."""
    return CACHE_DIR / "profiles"


def profile_dir(name: str) -> Path:
    """Return the profile directory for ``name`` (named profile or session key)."""
    return profiles_dir() / name


def state_path(session_key: str) -> Path:
    """Return the on-disk session-state JSON path for ``session_key``."""
    return CACHE_DIR / f"{session_key}.json"


def socket_path(session_key: str) -> str:
    """Return the Unix socket path for ``session_key``'s MCP daemon."""
    return str(CACHE_DIR / f"{session_key}.sock")


def daemon_pid_file(session_key: str) -> Path:
    """Return the daemon PID file path for ``session_key``."""
    return CACHE_DIR / f"{session_key}.daemon.pid"


def lock_path(session_key: str) -> Path:
    """Return the spawn-lock file path for ``session_key``."""
    return CACHE_DIR / f"{session_key}.lock"


# --------------------------------------------------------------------------- #
# Session key derivation
# --------------------------------------------------------------------------- #


def build_session_key(
    *,
    browser_url: str | None,
    isolated: bool,
    channel: str,
) -> str:
    """Build a stable key for the on-disk profile of a non-named session.

    One repository is one project is one browser: the key is derived from
    the resolved project root (git root, else cwd) and channel only, so a
    headed and a headless call - or an isolated and a persistent call - from
    the same project resolve to the same user-data-dir and reuse one Chrome
    instead of fragmenting into several and losing login state.

    ``isolated`` is accepted for backward compatibility but no longer
    participates in the key: throwaway and persistent sessions share the
    project bucket so auth survives. ``browser_url`` still partitions,
    because an externally attached Chrome is genuinely a different browser.
    ``channel`` partitions because different Chrome channels are different
    binaries that must not share a profile directory.

    Args:
        browser_url: Explicit remote debugging endpoint, if any.
        isolated: Ignored (kept for call-site compatibility).
        channel: Chrome channel.

    Returns:
        Deterministic short hash identifying the browser session bucket.
    """
    del isolated  # See docstring: no longer fragments the per-project bucket.
    raw_key = "|".join(
        [
            browser_url or "auto",
            get_project_id(),
            str(resolve_project_root()),
            channel,
        ]
    )
    return hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# File-level cleanup / enumeration
# --------------------------------------------------------------------------- #


def clear_session_files(session_key: str, *, keep_lock: bool = False) -> None:
    """Delete the runtime files belonging to a browser session.

    Args:
        session_key: Session key whose state, socket, and pid files should be
            removed.
        keep_lock: Leave the ``.lock`` file in place. Unlinking it while another
            process holds it breaks mutual exclusion - the next process creates
            a fresh inode and both then believe they hold the lock - so only
            callers discarding the profile entirely should remove it.

    Returns:
        None.
    """
    suffixes = (
        (".json", ".sock", ".daemon.pid")
        if keep_lock
        else (".json", ".sock", ".daemon.pid", ".lock")
    )
    for suffix in suffixes:
        (CACHE_DIR / f"{session_key}{suffix}").unlink(missing_ok=True)


def iter_session_state_paths() -> list[Path]:
    """List the browser-session state files under the cache directory.

    Returns:
        Sorted ``<session_key>.json`` paths, excluding the differently shaped
        ``browser_session_*`` overrides and ``active_attach_*`` records.
    """
    if not CACHE_DIR.exists():
        return []
    return sorted(
        path
        for path in CACHE_DIR.glob("*.json")
        if not path.name.startswith(("browser_session_", "active_attach_"))
    )
