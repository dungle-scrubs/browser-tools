"""Anonymous macOS Chrome sessions get the automatic 1Password setup (#201).

A temporary (unnamed) Chrome launch on macOS whose machine carries an
official source entry is provisioned and pin-seeded before the browser
starts, through the vendored launcher's generic directory-initializer hook.
The vendored launcher itself runs for real here: only the process spawn, the
CDP readiness probe and the startup sweep are stubbed, so these tests prove
the actual core hook and the generated files rather than a policy double.
The shared synthetic-source and codesign fixtures come from
``onepassword_lifecycle_fixtures``.
"""

from __future__ import annotations

import errno
import json
import stat
import sys
from pathlib import Path

import pytest
from onepassword_lifecycle_fixtures import DEST_NAME, HOSTS, OTHER_ORIGIN, STABLE
from onepassword_lifecycle_fixtures import _Boundaries as _Boundaries
from onepassword_lifecycle_fixtures import _darwin as _darwin
from onepassword_lifecycle_fixtures import (
    _never_read_the_real_registration as _never_read_the_real_registration,
)
from onepassword_lifecycle_fixtures import _no_real_camoufox_runner as _no_real_camoufox_runner
from onepassword_lifecycle_fixtures import _no_real_codesign as _no_real_codesign
from onepassword_lifecycle_fixtures import (
    fail_pin_default_create as fail_pin_default_create,
)
from onepassword_lifecycle_fixtures import (
    fail_pin_default_open as fail_pin_default_open,
)
from onepassword_lifecycle_fixtures import (
    fail_pin_publication as fail_pin_publication,
)
from onepassword_lifecycle_fixtures import fail_pin_stat as fail_pin_stat
from onepassword_lifecycle_fixtures import registry_path as registry_path
from onepassword_lifecycle_fixtures import (
    stubbed_core_boundaries as stubbed_core_boundaries,
)
from onepassword_lifecycle_fixtures import (
    synthetic_chrome_binary as synthetic_chrome_binary,
)
from onepassword_lifecycle_fixtures import valid_installation as valid_installation

from browser_tools import lifecycle, onepassword

PINNED_ID = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"


def _temporary_launch(registry_path: str, port: int) -> lifecycle.ExtendedInstance:
    return lifecycle.launch(
        headless=True, port=port, registry_path=registry_path, name="tempsession"
    )


class TestTemporarySessions:
    def test_two_launches_get_distinct_registered_sessions(
        self,
        registry_path: str,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        first = _temporary_launch(registry_path, 9461)
        second = _temporary_launch(registry_path, 9462)

        assert first.profile is None
        assert second.profile is None
        assert first.user_data_dir != second.user_data_dir
        sessions_root = stubbed_core_boundaries["sessions_root"]
        for info in (first, second):
            session_dir = Path(info.user_data_dir)
            assert session_dir.parent == sessions_root
            hosts = session_dir / HOSTS / DEST_NAME
            manifest = json.loads(hosts.read_text())
            assert manifest["allowed_origins"] == [STABLE]
            assert manifest["path"] == str(valid_installation)
            assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
            # The disposable session is registered unbound: stop and cleanup
            # keep treating it as throwaway.
            assert info.engine == "chrome"
        # The synthetic official source keeps its wider origin list.
        source = json.loads(onepassword.source_manifest_path().read_text())
        assert source["allowed_origins"] == [STABLE, OTHER_ORIGIN]

    def test_temporary_session_preferences_pin_the_stable_extension(
        self,
        registry_path: str,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        instance = _temporary_launch(registry_path, 9464)

        preferences_path = Path(instance.user_data_dir) / "Default" / "Preferences"
        preferences = json.loads(preferences_path.read_text())
        assert preferences["extensions"] == {"pinned_extensions": [PINNED_ID]}
        # The reviewed password-manager updates still land beside the pin.
        assert preferences["credentials_enable_service"] is False
        assert preferences["profile"]["password_manager_enabled"] is False
        assert stat.S_IMODE(preferences_path.stat().st_mode) == 0o600


class TestTemporarySetupBoundaries:
    def test_an_invalid_source_fails_before_chrome_starts_and_the_retry_is_safe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        invalid = tmp_path / HOSTS / DEST_NAME
        invalid.parent.mkdir(parents=True)
        invalid.write_text("{ not json")
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", invalid)

        with pytest.raises(lifecycle.LifecycleError, match="not valid JSON") as error:
            _temporary_launch(registry_path, 9465)

        message = str(error.value)
        assert "--1password" not in message
        assert "launch again to retry" in message
        # Nothing was spawned and no session directory was leaked.
        assert stubbed_core_boundaries["order"] == ["cleanup"]
        sessions_root = stubbed_core_boundaries["sessions_root"]
        assert list(sessions_root.glob("session-*")) == []

    def test_a_dangling_source_entry_fails_visibly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        source = tmp_path / HOSTS / DEST_NAME
        source.parent.mkdir(parents=True)
        source.symlink_to(tmp_path / "absent-target.json")
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", source)

        with pytest.raises(lifecycle.LifecycleError, match="could not be read as a regular file"):
            _temporary_launch(registry_path, 9466)

        assert stubbed_core_boundaries["order"] == ["cleanup"]

    def test_an_absent_source_keeps_a_plain_launch(
        self,
        registry_path: str,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        instance = _temporary_launch(registry_path, 9467)

        assert instance.profile is None
        session_dir = Path(instance.user_data_dir)
        assert not (session_dir / HOSTS).exists()
        assert "extensions" not in json.loads((session_dir / "Default" / "Preferences").read_text())
        # Plain launch: only preferences preparation ran before the spawn.
        assert stubbed_core_boundaries["order"] == ["cleanup", "preferences", "spawn"]

    def test_an_unsupported_platform_keeps_a_plain_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        instance = _temporary_launch(registry_path, 9468)

        assert instance.profile is None
        assert not (Path(instance.user_data_dir) / HOSTS).exists()
        assert stubbed_core_boundaries["order"] == ["cleanup", "preferences", "spawn"]

    def test_an_anonymous_camoufox_launch_never_takes_an_initializer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        valid_installation: Path,
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(lifecycle, "EPHEMERAL_ROOT", str(tmp_path / "ephemeral"))

        def fake_camoufox(**kwargs: object) -> lifecycle.ExtendedInstance:
            captured.update(kwargs)
            return lifecycle.ExtendedInstance(
                name="cam-01",
                port=9469,
                pid=711,
                browser_version="camoufox",
                user_data_dir=str(kwargs["user_data_dir"]),
                launched=None,
                pid_start=None,
                engine="camoufox",
                profile=None,
            )

        monkeypatch.setattr(lifecycle, "_launch_camoufox", fake_camoufox)
        instance = lifecycle.launch(engine="camoufox", registry_path=registry_path)

        assert instance.engine == "camoufox"
        assert instance.profile is None
        # The ephemeral directory is created, but no setup ever touched it.
        ephemeral = Path(str(captured["user_data_dir"]))
        assert ephemeral.is_dir()
        assert not (ephemeral / HOSTS).exists()
        assert not (ephemeral / "Default" / "Preferences").exists()


_SEED_BOUNDARIES = {
    "verify": lambda monkeypatch: fail_pin_stat(
        monkeypatch, OSError(errno.EACCES, "synthetic verify failure")
    ),
    "default-open": lambda monkeypatch: fail_pin_default_open(
        monkeypatch, OSError(errno.EACCES, "synthetic default read failure")
    ),
    "default-create": lambda monkeypatch: fail_pin_default_create(
        monkeypatch, OSError(errno.EACCES, "synthetic default create failure")
    ),
    "staging": lambda monkeypatch: fail_pin_publication(
        monkeypatch, "staging", OSError(errno.EACCES, "synthetic seed staging failure")
    ),
    "link": lambda monkeypatch: fail_pin_publication(
        monkeypatch, "link", OSError(errno.EPERM, "synthetic seed link failure")
    ),
}


class TestTemporarySeedBoundaries:
    """A temporary session whose seed cannot be vouched for aborts closed.

    The real provisioner runs first; only the seed's own filesystem boundary
    fails, synthetically. The abort must happen before any spawn, remove the
    owned throwaway directory, and keep the anonymous retry advice honest:
    the failed session is never reused, whether its cleanup removed it or
    retained it.
    """

    @pytest.mark.parametrize("boundary", sorted(_SEED_BOUNDARIES))
    def test_a_seed_boundary_failure_aborts_and_removes_the_owned_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        boundary: str,
        registry_path: str,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: _Boundaries,
    ) -> None:
        _SEED_BOUNDARIES[boundary](monkeypatch)

        # The setup module owns its error below the lifecycle layer; the
        # launch boundary translates it to a LifecycleError (CLI exit 1)
        # keeping the path-only reason and the retry advice verbatim.
        with pytest.raises(lifecycle.LifecycleError, match=r"synthetic .* failure") as error:
            _temporary_launch(registry_path, 9475)

        message = str(error.value)
        assert message.startswith("Launch failed: ")
        # Path-only reason plus the anonymous remedy; the named-profile flag
        # advice must not appear for a session that cannot use it.
        assert "/session-" in message
        assert "Launch again to retry the temporary setup." in message
        assert "--1password" not in message
        # Nothing was spawned, no preferences were prepared, and the owned
        # throwaway setup was removed rather than leaked.
        assert stubbed_core_boundaries["order"] == ["cleanup"]
        sessions_root = stubbed_core_boundaries["sessions_root"]
        assert list(sessions_root.glob("session-*")) == []
