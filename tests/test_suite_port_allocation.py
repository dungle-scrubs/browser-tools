"""The suite's own browsers must never land on the default DevTools port.

The regression this file exists for. ``core.registry.allocate_port`` probes
upward from ``BASE_PORT``, which is 9222, so the first browser the suite
launched took 9222 whenever nothing else held it. ``tests/conftest.py`` refuses
any test that speaks DevTools to 9222, because on a developer machine that is a
browser they are using -- so the suite's own fixture endpoint was refused and
25 tests failed.

The failure was invisible for as long as a developer's own instance happened to
hold 9222: the suite quietly got 9223 and up. It appeared the day that instance
went away, on a tree that had not changed, which is the worst shape a test bug
can have.
"""

from __future__ import annotations

from browser_tools.core import registry

#: Kept in step with ``tests/conftest.py`` deliberately rather than imported:
#: ``tests`` is not a package, and the value this file defends is the literal
#: default DevTools port, not whatever conftest happens to say today.
FORBIDDEN_PORT = 9222


def test_the_suite_allocates_above_the_forbidden_port():
    assert registry.BASE_PORT > FORBIDDEN_PORT
    assert registry.MAX_PORT > registry.BASE_PORT


def test_an_empty_registry_never_allocates_the_forbidden_port():
    assert registry.allocate_port({}) != FORBIDDEN_PORT
    assert registry.allocate_port({}) > FORBIDDEN_PORT


def test_the_whole_allocation_range_clears_the_forbidden_port():
    """Not just the first port: no port the suite can be handed is 9222."""
    assert FORBIDDEN_PORT < registry.BASE_PORT
    assert not (registry.BASE_PORT <= FORBIDDEN_PORT <= registry.MAX_PORT)
