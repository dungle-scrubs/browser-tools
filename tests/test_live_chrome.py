"""Unit tests for live_chrome.resolve_live_chrome.

Patches land at ``browser_tools.process_utils.<fn>`` - the single canonical
patch site, because resolve_live_chrome reaches every primitive through the
process_utils module object. ``read_singleton_lock_pid`` is left real so the
on-disk SingletonLock symlink drives the lock-read step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from browser_tools.live_chrome import LiveChrome, resolve_live_chrome

if TYPE_CHECKING:
    from pathlib import Path

PROC = "browser_tools.process_utils"


def _symlink_lock(user_data_dir: Path, pid: int) -> None:
    """Create a Chrome-style SingletonLock symlink pointing at ``pid``."""
    (user_data_dir / "SingletonLock").symlink_to(f"host-{pid}")


def test_returns_none_when_no_lock(tmp_path: Path) -> None:
    assert resolve_live_chrome(tmp_path) is None


def test_returns_none_when_lock_pid_dead(tmp_path: Path) -> None:
    _symlink_lock(tmp_path, 1)
    with patch(f"{PROC}.is_process_alive", return_value=False):
        assert resolve_live_chrome(tmp_path) is None


def test_no_debug_port_flag(tmp_path: Path) -> None:
    _symlink_lock(tmp_path, 99999)
    with (
        patch(f"{PROC}.is_process_alive", return_value=True),
        patch(f"{PROC}.find_chrome_debug_port", return_value=None),
    ):
        chrome = resolve_live_chrome(tmp_path)
    assert chrome == LiveChrome(
        pid=99999,
        holds_dir=None,
        intended_port=None,
        port=None,
        endpoint=None,
        devtools_alive=False,
        port_collision_pids=[],
    )


def test_devtools_reachable(tmp_path: Path) -> None:
    _symlink_lock(tmp_path, 99999)
    with (
        patch(f"{PROC}.is_process_alive", return_value=True),
        patch(f"{PROC}.find_chrome_debug_port", return_value=9222),
        patch(f"{PROC}.is_devtools_available", return_value=True),
    ):
        chrome = resolve_live_chrome(tmp_path)
    assert chrome is not None
    assert chrome.pid == 99999
    assert chrome.devtools_alive is True
    assert chrome.port == 9222
    assert chrome.endpoint == "http://127.0.0.1:9222"
    assert chrome.port_collision_pids == []


def test_devtools_down_reports_collisions_excluding_holder(tmp_path: Path) -> None:
    _symlink_lock(tmp_path, 99999)
    with (
        patch(f"{PROC}.is_process_alive", return_value=True),
        patch(f"{PROC}.find_chrome_debug_port", return_value=9222),
        patch(f"{PROC}.is_devtools_available", return_value=False),
        # Holder (99999) appears in the listener list and must be excluded.
        patch(f"{PROC}.find_listeners_on_port", return_value=[99999, 11111, 22222]),
    ):
        chrome = resolve_live_chrome(tmp_path)
    assert chrome is not None
    assert chrome.devtools_alive is False
    assert chrome.port is None
    assert chrome.endpoint is None
    assert chrome.port_collision_pids == [11111, 22222]


def test_holds_dir_unverified_by_default(tmp_path: Path) -> None:
    _symlink_lock(tmp_path, 99999)
    with (
        patch(f"{PROC}.is_process_alive", return_value=True),
        patch(f"{PROC}.find_chrome_debug_port", return_value=None),
    ):
        chrome = resolve_live_chrome(tmp_path)
    assert chrome is not None
    assert chrome.holds_dir is None


def test_verify_holds_dir_true_when_dir_matches(tmp_path: Path) -> None:
    _symlink_lock(tmp_path, 99999)
    with (
        patch(f"{PROC}.is_process_alive", return_value=True),
        patch(f"{PROC}.find_chrome_debug_port", return_value=None),
        patch(f"{PROC}.find_chrome_user_data_dir", return_value=tmp_path),
    ):
        chrome = resolve_live_chrome(tmp_path, verify_holds_dir=True)
    assert chrome is not None
    assert chrome.holds_dir is True


def test_verify_holds_dir_false_when_pid_points_elsewhere(tmp_path: Path) -> None:
    """A recycled SingletonLock PID running a different dir is not holding it."""
    _symlink_lock(tmp_path, 99999)
    other = tmp_path / "elsewhere"
    with (
        patch(f"{PROC}.is_process_alive", return_value=True),
        patch(f"{PROC}.find_chrome_debug_port", return_value=None),
        patch(f"{PROC}.find_chrome_user_data_dir", return_value=other),
    ):
        chrome = resolve_live_chrome(tmp_path, verify_holds_dir=True)
    assert chrome is not None
    assert chrome.holds_dir is False
