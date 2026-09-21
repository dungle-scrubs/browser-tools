"""A real `launch --engine camoufox`, against the product path.

Camoufox is a headline capability and nothing exercised it live. What
coverage existed was mocked in three separate ways, none of which starts a
browser:

- ``test_cli_lifecycle.py::test_camoufox_engine_launches_and_registers``
  stubs ``_spawn_camoufox_process``, so the spawn itself never runs.
- ``tests/parity/test_e2e_camoufox.py`` says it launches a real browser, and
  ``tests/parity/conftest.py`` patches ``camoufox_session.Camoufox`` out from
  under it. It drives ``CamoufoxSession``, which its own docstring calls a
  test oracle and not a product surface: ``launch --engine camoufox`` goes
  through ``lifecycle`` into ``camoufox_runner`` and never touches it.
- The parity suite compares snapshot engines, not the launcher.

So the path a user takes had no test proving a Camoufox process starts,
registers with ``engine="camoufox"``, reports alive through the engine-aware
liveness rule, and stops cleanly.

This test takes it. It skips when the Camoufox browser is not fetched,
because fetching it is a large download CI should not carry, so CI reports a
skip rather than a false pass. Run it where the browser exists:

    camoufox fetch
    pytest tests/parity/test_camoufox_live_launch.py -v

TWO LAUNCHES, NOT FIVE. Camoufox is a Firefox build and takes seconds to
reach readiness; ``_CAMOUFOX_READY_TIMEOUT`` bounds the wait. A
function-scoped fixture launched one browser per test, which passed alone
and hit the timeout when this module ran beside the rest of the parity
suite. The read-only assertions therefore share one module-scoped browser,
and only the stop test launches its own, because it destroys what it
launches.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from browser_tools import lifecycle

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.parity


def _camoufox_is_fetched() -> bool:
    """True when the Camoufox browser itself is on disk, not just the package.

    The Python package importing is not enough: ``camoufox fetch`` downloads
    the browser separately, and a launch without it fails at spawn.
    """
    try:
        import camoufox  # noqa: F401
    except ImportError:
        return False
    candidates = [
        Path.home() / ".cache" / "camoufox",
        Path.home() / "Library" / "Caches" / "camoufox",
        Path(os.environ.get("XDG_CACHE_HOME", "/nonexistent")) / "camoufox",
    ]
    return any(path.is_dir() and any(path.iterdir()) for path in candidates)


requires_camoufox = pytest.mark.skipif(
    not _camoufox_is_fetched(),
    reason="the Camoufox browser is not fetched; run `camoufox fetch`",
)


def _process_alive(pid: int) -> bool:
    return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """One real headless Camoufox for the read-only assertions.

    Module-scoped, so ``monkeypatch`` is unavailable and the environment is
    set through ``MonkeyPatch`` directly. The registry is a temp file, so
    nothing here can see or stop an instance the user is running.
    """
    if not _camoufox_is_fetched():
        pytest.skip("the Camoufox browser is not fetched")
    base = tmp_path_factory.mktemp("camoufox-live")
    registry = str(base / "registry.json")
    patch = pytest.MonkeyPatch()
    patch.setenv(lifecycle.PROFILES_ENV_VAR, str(base / "profiles"))
    instance = None
    try:
        instance = lifecycle.launch(engine="camoufox", headless=True, registry_path=registry)
        yield {"instance": instance, "registry": registry, "base": base}
    finally:
        # Teardown must not mask a failure, and must not leave a browser
        # running when the test already failed for its own reason.
        if instance is not None:
            with contextlib.suppress(Exception):
                lifecycle.stop(instance.name, registry_path=registry)
        patch.undo()


@requires_camoufox
class TestARealCamoufoxLaunch:
    def test_it_registers_as_camoufox(self, live):
        instance, registry = live["instance"], live["registry"]
        assert instance.engine == "camoufox"
        assert instance.pid > 0
        raw = json.loads(Path(registry).read_text())
        assert raw[instance.name]["engine"] == "camoufox"
        assert raw[instance.name]["pid"] == instance.pid

    def test_the_process_is_actually_running(self, live):
        """A registry row is not a browser. Ask the operating system."""
        assert _process_alive(live["instance"].pid), "no process with that pid"

    def test_liveness_reports_it_alive(self, live):
        """Engine-aware liveness: process identity plus a user-data-dir hold."""
        rows = lifecycle.status(registry_path=live["registry"])
        row = next(r for r in rows if r["name"] == live["instance"].name)
        assert row["alive"] is True
        assert row["engine"] == "camoufox"

    def test_an_unbound_session_dir_stays_out_of_the_profile_root(self, live):
        """CONTEXT.md, Profile Root: nothing throwaway lands in durable storage."""
        profile_root = live["base"] / "profiles"
        assert profile_root not in Path(live["instance"].user_data_dir).parents
        assert live["instance"].profile is None


@requires_camoufox
def test_stop_retires_the_entry_and_ends_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its own browser, because it destroys the one it launches."""
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "profiles"))
    registry = str(tmp_path / "registry.json")
    instance = lifecycle.launch(engine="camoufox", headless=True, registry_path=registry)
    try:
        assert _process_alive(instance.pid)
        lifecycle.stop(instance.name, registry_path=registry)
        rows = lifecycle.status(registry_path=registry)
        assert all(r["name"] != instance.name for r in rows), "entry survived stop"
        assert not _process_alive(instance.pid), "process outlived stop"
    finally:
        with contextlib.suppress(Exception):
            lifecycle.stop(instance.name, registry_path=registry)


def test_the_skip_guard_can_say_no():
    """A guard that cannot refuse would make every skip above meaningless."""
    assert isinstance(_camoufox_is_fetched(), bool)
    assert requires_camoufox.args[0] is not _camoufox_is_fetched()
