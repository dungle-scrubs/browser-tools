"""A closing browser window must not destroy the login inside it (#94).

A headed ``launch --profile NAME`` starts one detached supervisor. When the
browser goes away the supervisor retires the instance, and retirement used to
run through the vendored ``deregister``, which removes the entry and then
retries ``rmtree`` for six seconds against whatever path the entry recorded --
checking neither the entry's ``profile`` field nor whether the path lies under
a session root. A named profile was destroyed about two seconds after a normal
close, while ``bt stop`` preserved it, so the failure looked like the login
vanishing on its own.

``core/registry.py`` is verbatim vendored, so the fix is at the call site:
retirement goes through ``lifecycle.retire_instance``. With both the stop path
and the retirement path preserving a bound profile, the order they run in stops
mattering, so the race between them is closed by construction.

These tests drive real retirement. The pre-existing supervisor tests stub out
process termination and exercise only the branch that already worked.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from browser_tools import lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.core import supervisor

if TYPE_CHECKING:
    from pathlib import Path


def _registry_with(
    tmp_path: Path, *, name: str, profile: str | None
) -> tuple[str, Path, str]:
    """Register one instance; return its registry path, user-data dir and name."""
    registry_path = str(tmp_path / "registry.json")
    user_data_dir = tmp_path / ("profile-" + name if profile else "session-" + name)
    user_data_dir.mkdir()
    (user_data_dir / "Cookies").write_bytes(b"login-state")

    info = core_registry.register(
        working_dir=str(tmp_path / name),
        pid=424242,
        browser_version="Chrome/153.0.0.0",
        user_data_dir=str(user_data_dir),
        port_override=9333,
        registry_path=registry_path,
    )
    lifecycle.annotate_entry(
        info.name,
        engine="chrome",
        profile=profile,
        registry_path=registry_path,
    )
    return registry_path, user_data_dir, info.name


class TestRetirementPreservesAProfile:
    """Retiring a profile-bound instance keeps the directory."""

    def test_a_bound_profile_survives_retirement(self, tmp_path: Path) -> None:
        registry_path, user_data_dir, name = _registry_with(
            tmp_path, name="web-01", profile="dev"
        )

        lifecycle.retire_instance(instance_name=name, registry_path=registry_path)

        assert user_data_dir.exists(), "retirement deleted a named profile"
        assert (user_data_dir / "Cookies").read_bytes() == b"login-state"
        assert lifecycle.read_instances(registry_path) == []

    def test_an_unbound_session_dir_is_still_reclaimed(self, tmp_path: Path) -> None:
        """The fix must not turn ephemeral sessions into orphans."""
        registry_path, user_data_dir, name = _registry_with(
            tmp_path, name="web-02", profile=None
        )

        lifecycle.retire_instance(instance_name=name, registry_path=registry_path)

        assert not user_data_dir.exists(), "an unbound session dir was left behind"
        assert lifecycle.read_instances(registry_path) == []

    def test_retiring_an_unknown_instance_is_a_no_op(self, tmp_path: Path) -> None:
        """Idempotent, so it stays safe to race with stop() and cleanup()."""
        registry_path = str(tmp_path / "registry.json")
        core_registry._save_registry({}, registry_path)  # pyright: ignore[reportPrivateUsage]

        assert (
            lifecycle.retire_instance(
                instance_name="never-registered", registry_path=registry_path
            )
            is False
        )


class TestTheStopRace:
    """Whichever of stop and retirement runs first, the profile survives."""

    def test_stop_then_retirement(self, tmp_path: Path) -> None:
        registry_path, user_data_dir, name = _registry_with(
            tmp_path, name="web-03", profile="dev"
        )

        lifecycle._remove_entry(  # pyright: ignore[reportPrivateUsage]
            name,
            registry_path,
            reap_dir=False,
            user_data_dir=str(user_data_dir),
        )
        lifecycle.retire_instance(instance_name=name, registry_path=registry_path)

        assert user_data_dir.exists()
        assert (user_data_dir / "Cookies").read_bytes() == b"login-state"

    def test_retirement_then_stop(self, tmp_path: Path) -> None:
        registry_path, user_data_dir, name = _registry_with(
            tmp_path, name="web-04", profile="dev"
        )

        lifecycle.retire_instance(instance_name=name, registry_path=registry_path)
        lifecycle._remove_entry(  # pyright: ignore[reportPrivateUsage]
            name,
            registry_path,
            reap_dir=False,
            user_data_dir=str(user_data_dir),
        )

        assert user_data_dir.exists()
        assert (user_data_dir / "Cookies").read_bytes() == b"login-state"


class TestTheSupervisorIsWiredToTheLifecycleOperation:
    """The supervisor must not reach the vendored deletion path at all."""

    def test_supervisor_retires_through_lifecycle(self) -> None:
        source = inspect.getsource(supervisor)
        assert "from .registry import deregister" not in source, (
            "the supervisor still imports the vendored deregister, which deletes "
            "the recorded user_data_dir without checking the profile field"
        )
        assert "retire_instance" in source, (
            "the supervisor must retire through lifecycle.retire_instance"
        )

    def test_the_vendored_deregister_is_unchanged(self) -> None:
        """The defect is corrected at the call site, not inside vendored code."""
        source = inspect.getsource(core_registry.deregister)
        assert "_remove_session_dir(session_dir)" in source, (
            "core/registry.py is verbatim vendored and must not be edited"
        )
