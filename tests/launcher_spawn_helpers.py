"""Shared spawn-boundary reject guards for the launcher test doubles.

The suite's spawn doubles refuse any environment that is not the synthetic
mapping the ``synthetic_spawn_environment`` fixture stubs the copy boundary
with. That reject guard used to live in ``tests/conftest.py``, but importing
a helper from ``conftest`` by flat name is ambiguous in a whole-suite run:
``tests/parity/conftest.py`` puts its own directory on ``sys.path``, so
``import conftest`` resolves to the parity conftest there and the import
fails. A uniquely named support module keeps the same import style as the
other flat-named test modules (``onepassword_lifecycle_fixtures``) with no
name to collide with.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def stub_spawn_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stub the OS-environment copy boundary to one synthetic mapping.

    ``launch_browser`` builds the spawned process's environment with
    ``os.environ.copy()``. A spawn double that receives that mapping holds a
    copy of the developer's real environment, and a failing assertion on it
    prints the values into the failure trace (round-4 tests, #211). The stub
    replaces the copy boundary itself with a small known mapping, so no real
    value is read, copied, or printed.

    Args:
        monkeypatch: The test's monkeypatch, which restores the copy
            boundary when the test ends.

    Returns:
        The synthetic mapping. Each ``os.environ.copy()`` call returns a new
        ``dict`` equal to it, so a launch that adds a key never changes it.
    """
    mapping = {"BROWSER_TOOLS_TEST_ENV": "synthetic"}
    monkeypatch.setattr(os.environ, "copy", lambda: dict(mapping))
    return mapping


def discard_spawn_environment(env: object, synthetic: dict[str, str]) -> None:
    """Drop a spawned process's environment before any assertion can print it.

    The reject guard for every spawn double: the copy boundary is stubbed to
    ``synthetic`` (see the ``synthetic_spawn_environment`` fixture), so an
    environment that is not exactly that mapping means the real-copy path was
    reached. The refusal names no values - it never prints or compares the
    received environment against the real one, it only refuses it.

    Args:
        env: The ``env`` the launch handed the spawn boundary, if any.
        synthetic: The one synthetic mapping the copy boundary may produce.

    Raises:
        AssertionError: The environment is neither absent nor the synthetic
            mapping, so a real environment reached a spawn double.
    """
    if env is not None and env != synthetic:
        raise AssertionError(
            "a spawn double received an environment that is not the synthetic "
            "mapping; the OS-environment copy boundary was not stubbed"
        )
