"""Production launches must allocate above the default DevTools port.

E55: PR #171 moved the test suite's allocation base to 9422, but production
still allocated from 9222 upward -- the vendored
``core.registry.allocate_port`` probes from upstream chrome-agent's
``BASE_PORT``, which is the DevTools default. On a machine where 9222 is
free, ``bt launch`` parked its instance on the one port every rule in this
project treats as "a browser this tool did not launch", and the defect stayed
invisible for as long as a developer's own Chrome happened to hold the port.

``src/browser_tools/core/registry.py`` is verbatim (RFC-01, "Vendoring
rules"), so the correction lives at the call site: ``lifecycle`` raises
``BASE_PORT``/``MAX_PORT`` for the duration of each vendored allocation
(``_safe_allocation_base``). These tests pin that behaviour.

The suite-wide fixture in ``tests/conftest.py`` already sets the module
globals to 9422 for the whole session, which would mask a removed or broken
wrapper: every assertion here would still see 9422. So each test first
restores the upstream defaults (9222/9322) inside its own scope, the state
production starts in, and asserts the wrapper -- not the session fixture --
is what clears the port.
"""

from __future__ import annotations

import pytest

from browser_tools import lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError

#: The literal this file defends, kept here rather than imported: the value
#: is the DevTools default port, not whatever another module happens to say.
UPSTREAM_BASE_PORT = 9222

#: In step with ``tests/conftest.py`` TEST_BASE_PORT and
#: ``lifecycle.ALLOCATION_BASE_PORT`` so one number keeps one meaning.
PRODUCTION_BASE_PORT = 9422


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


@pytest.fixture
def upstream_defaults(monkeypatch):
    """Restore the vendored module to its upstream 9222 defaults.

    Production runs with these values; only the call-site wrapper lifts them.
    """
    monkeypatch.setattr(core_registry, "BASE_PORT", UPSTREAM_BASE_PORT)
    monkeypatch.setattr(core_registry, "MAX_PORT", UPSTREAM_BASE_PORT + 100)


class TestChromeLaunch:
    def test_launch_allocates_above_the_default_port(
        self, registry_path, monkeypatch, upstream_defaults
    ):
        """The vendored launcher runs with the allocation base lifted.

        The fake replaces only ``launch_browser``'s browser half: it lets the
        real vendored ``register`` allocate (``port_override=None``) under
        whatever base the wrapper set, so both the captured globals and the
        port that reached the registry are asserted.
        """
        captured = {}

        async def fake_launch_browser(**kwargs):
            captured["base"] = core_registry.BASE_PORT
            captured["max"] = core_registry.MAX_PORT
            core_registry.register(
                working_dir="/tmp/my-site",
                pid=4321,
                browser_version="Chrome/9",
                user_data_dir="/tmp/session-x",
                port_override=None,
                registry_path=kwargs["registry_path"],
                pid_start="tok",
            )
            return core_registry.lookup("my-site-01", registry_path=kwargs["registry_path"])

        monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", fake_launch_browser)

        inst = lifecycle.launch(engine="chrome", registry_path=registry_path)

        assert captured["base"] == PRODUCTION_BASE_PORT
        assert captured["max"] == PRODUCTION_BASE_PORT + 100
        assert inst.port >= PRODUCTION_BASE_PORT

    def test_explicit_port_is_passed_through_unchanged(
        self, registry_path, monkeypatch, upstream_defaults
    ):
        """--port pins an exact port and is not second-guessed."""
        captured = {}

        async def fake_launch_browser(**kwargs):
            captured["port_override"] = kwargs["port_override"]
            core_registry.register(
                working_dir="/tmp/my-site",
                pid=4321,
                browser_version="Chrome/9",
                user_data_dir="/tmp/session-x",
                port_override=9777,
                registry_path=kwargs["registry_path"],
                pid_start="tok",
            )
            return core_registry.lookup("my-site-01", registry_path=kwargs["registry_path"])

        monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", fake_launch_browser)

        inst = lifecycle.launch(engine="chrome", port=9777, registry_path=registry_path)

        assert captured["port_override"] == 9777
        assert inst.port == 9777

    def test_a_failed_launch_restores_the_module_globals(
        self, registry_path, monkeypatch, upstream_defaults
    ):
        async def boom(**kwargs):
            raise RuntimeError("Chrome exited immediately")

        monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", boom)

        with pytest.raises(LifecycleError):
            lifecycle.launch(engine="chrome", registry_path=registry_path)

        assert core_registry.BASE_PORT == UPSTREAM_BASE_PORT
        assert core_registry.MAX_PORT == UPSTREAM_BASE_PORT + 100


class TestCamoufoxLaunch:
    def test_registration_allocates_above_the_default_port(
        self, registry_path, monkeypatch, tmp_path, upstream_defaults
    ):
        """Camoufox registers through the real vendored ``register``.

        The detached runner is stubbed so no browser starts; the vendored
        ``register`` runs for real under the wrapper and its chosen port is
        asserted, so this covers the whole allocation path, not a capture.
        """
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "profiles"))
        monkeypatch.setattr(lifecycle, "EPHEMERAL_ROOT", str(tmp_path / "ephemeral"))
        monkeypatch.setattr(
            lifecycle, "_spawn_camoufox_process", lambda user_data_dir, headless: (7777, "tok")
        )

        inst = lifecycle.launch(engine="camoufox", registry_path=registry_path)

        assert inst.engine == "camoufox"
        assert inst.port >= PRODUCTION_BASE_PORT
        assert core_registry.BASE_PORT == UPSTREAM_BASE_PORT
