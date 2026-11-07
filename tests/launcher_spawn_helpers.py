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
