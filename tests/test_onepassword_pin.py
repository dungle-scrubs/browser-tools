"""The new-profile pin default: seed a missing Preferences, touch nothing else.

Seeding writes one static JSON document, ``extensions.pinned_extensions``
naming the stable 1Password extension, into a ``Default/Preferences`` file
that does not exist yet. Any entry already at that name - a document with a
pin choice, an empty list, a symlink - is left byte-for-byte alone, and no
symlink on the path is followed. The policy tests run the vendored launcher
for real through the shared stubbed-boundary fixtures (#201).
"""

from __future__ import annotations

import errno
import json
import os
import stat
from typing import TYPE_CHECKING, Any

import pytest
from onepassword_lifecycle_fixtures import DEST_NAME, HOSTS
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
from onepassword_lifecycle_fixtures import profiles_root as profiles_root
from onepassword_lifecycle_fixtures import registry_path as registry_path
from onepassword_lifecycle_fixtures import (
    stubbed_core_boundaries as stubbed_core_boundaries,
)
from onepassword_lifecycle_fixtures import (
    synthetic_chrome_binary as synthetic_chrome_binary,
)
from onepassword_lifecycle_fixtures import valid_installation as valid_installation

from browser_tools import lifecycle, onepassword, onepassword_setup

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PINNED_ID = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"

PIN_DOCUMENT = {"extensions": {"pinned_extensions": [PINNED_ID]}}


def _preferences(profile: Path) -> Path:
    return profile / "Default" / "Preferences"


def _fail_os_open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    matches: Callable[[object, dict[str, object]], bool],
    error: OSError,
) -> None:
    """Make ``os.open`` raise ``error`` for the calls ``matches`` selects.

    Unrelated real opens - including the test machinery's own - keep working,
    so a failing witness proves the abort, not a broken harness.
    """
    real_open = os.open

    def failing(path: Any, flags: int, *args: Any, **kwargs: Any) -> object:
        if matches(path, kwargs):
            raise error
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)


def _no_dir_fd(kwargs: dict[str, object]) -> bool:
    return kwargs.get("dir_fd") is None


class TestSeedPin:
    def test_a_missing_preferences_file_is_created_with_the_static_document(
        self, tmp_path: Path
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()

        onepassword_setup.seed_pin(session)

        created = _preferences(session)
        assert json.loads(created.read_text()) == PIN_DOCUMENT
        assert stat.S_IMODE(created.stat().st_mode) == 0o600

    def test_the_pinned_id_is_derived_from_the_validated_stable_origin(self) -> None:
        derived = onepassword_setup.stable_extension_id()
        assert derived == PINNED_ID
        assert f"chrome-extension://{derived}/" == onepassword.STABLE_EXTENSION_ORIGIN

    @pytest.mark.parametrize(
        "existing",
        [
            {"extensions": {"pinned_extensions": []}},
            {"extensions": {"pinned_extensions": ["other" * 8]}},
            {
                "homepage": "https://example.test/",
                "browser": {"show_home_button": True},
                "unrelated": {"deep": ["values", 1, None]},
            },
        ],
    )
    def test_an_existing_preferences_document_is_never_rewritten(
        self, tmp_path: Path, existing: dict[str, object]
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        original = json.dumps(existing, indent=3)
        _preferences(session).parent.mkdir(parents=True)
        _preferences(session).write_text(original)

        onepassword_setup.seed_pin(session)

        assert _preferences(session).read_text() == original

    def test_a_preferences_symlink_is_never_followed(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        session.mkdir()
        outside = tmp_path / "outside-preferences"
        outside.write_text('{"extensions": {"pinned_extensions": []}}')
        default = session / "Default"
        default.mkdir()
        (default / "Preferences").symlink_to(outside)

        onepassword_setup.seed_pin(session)

        assert (default / "Preferences").is_symlink()
        assert outside.read_text() == '{"extensions": {"pinned_extensions": []}}'

    def test_a_default_symlink_is_never_followed(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        session.mkdir()
        outside = tmp_path / "outside-default"
        outside.mkdir()
        (session / "Default").symlink_to(outside)

        onepassword_setup.seed_pin(session)

        assert not (outside / "Preferences").exists()

    def test_a_publication_collision_keeps_the_existing_choice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        winner = '{"extensions": {"pinned_extensions": ["cccccccccccccccccccccccccccccccc"]}}'
        real_link = os.link

        def link_then_publish(src, dst, *args, **kwargs):
            default = session / "Default"
            (default / "Preferences").write_text(winner)
            return real_link(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "link", link_then_publish)

        onepassword_setup.seed_pin(session)

        assert (_preferences(session).read_text()) == winner
        leftovers = [
            entry.name
            for entry in (_preferences(session).parent).iterdir()
            if entry.name != "Preferences"
        ]
        assert leftovers == []

    def test_a_failed_publication_cleans_only_its_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()

        def failing_fsync(fd):
            raise OSError(5, "synthetic seed I/O failure")

        monkeypatch.setattr(os, "fsync", failing_fsync)

        with pytest.raises(onepassword_setup.SessionSetupError, match="synthetic seed I/O"):
            onepassword_setup.seed_pin(session)

        assert not _preferences(session).exists()
        assert list((_preferences(session).parent).iterdir()) == []

    def test_a_root_read_failure_aborts_path_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        _fail_os_open(
            monkeypatch,
            matches=lambda path, kwargs: _no_dir_fd(kwargs) and path == session,
            error=OSError(errno.EACCES, "synthetic root read failure"),
        )

        with pytest.raises(
            onepassword_setup.SessionSetupError, match="synthetic root read failure"
        ) as error:
            onepassword_setup.seed_pin(session)

        assert str(session) in str(error.value)
        assert "for the 1Password extension pin" in str(error.value)
        assert not (session / "Default").exists()

    def test_a_default_read_failure_aborts_path_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        (session / "Default").mkdir()
        _fail_os_open(
            monkeypatch,
            matches=lambda path, kwargs: kwargs.get("dir_fd") is not None and path == "Default",
            error=OSError(errno.EACCES, "synthetic default read failure"),
        )

        with pytest.raises(
            onepassword_setup.SessionSetupError, match="synthetic default read failure"
        ) as error:
            onepassword_setup.seed_pin(session)

        assert str(session) in str(error.value)
        assert not _preferences(session).exists()

    def test_a_default_preparation_failure_aborts_path_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        real_mkdir = os.mkdir

        def failing_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("dir_fd") is not None and path == "Default":
                raise OSError(errno.EACCES, "synthetic default create failure")
            real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", failing_mkdir)

        with pytest.raises(
            onepassword_setup.SessionSetupError, match="synthetic default create failure"
        ) as error:
            onepassword_setup.seed_pin(session)

        assert str(session / "Default") in str(error.value)
        assert list(session.iterdir()) == []

    @pytest.mark.parametrize("failure_errno", [errno.EACCES, errno.EPERM, errno.EIO])
    def test_only_a_provably_absent_target_permits_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_errno: int
    ) -> None:
        """FileNotFoundError is the only absence proof; any other stat
        failure aborts the seed instead of silently skipping it."""
        session = tmp_path / "session"
        session.mkdir()
        (session / "Default").mkdir()
        real_stat = os.stat

        def failing_stat(path: Any, *args: Any, **kwargs: Any):
            if kwargs.get("dir_fd") is not None and path == "Preferences":
                raise OSError(failure_errno, f"synthetic verify failure {failure_errno}")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", failing_stat)

        with pytest.raises(
            onepassword_setup.SessionSetupError, match=f"synthetic verify failure {failure_errno}"
        ) as error:
            onepassword_setup.seed_pin(session)

        assert "Cannot verify the pin target" in str(error.value)
        assert str(session) in str(error.value)
        assert not _preferences(session).exists()
        assert list((session / "Default").iterdir()) == []

    @pytest.mark.parametrize(
        ("kind", "message"),
        [("staging", "synthetic seed staging failure"), ("link", "synthetic seed link failure")],
    )
    def test_publication_boundaries_abort_path_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
        message: str,
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        if kind == "staging":
            _fail_os_open(
                monkeypatch,
                matches=lambda path, kwargs: (
                    kwargs.get("dir_fd") is not None and str(path).startswith(".Preferences-")
                ),
                error=OSError(errno.EACCES, message),
            )
        else:
            real_link = os.link

            def failing_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
                if kwargs.get("src_dir_fd") is not None and dst == "Preferences":
                    raise OSError(errno.EPERM, message)
                real_link(src, dst, *args, **kwargs)

            monkeypatch.setattr(os, "link", failing_link)

        with pytest.raises(onepassword_setup.SessionSetupError, match=message) as error:
            onepassword_setup.seed_pin(session)

        assert "Cannot seed the 1Password extension pin" in str(error.value)
        assert str(session) in str(error.value)
        # No half-published document and no staging leftovers remain.
        assert not _preferences(session).exists()
        assert list((session / "Default").iterdir()) == []

    def test_a_verification_failure_never_touches_an_existing_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = tmp_path / "session"
        session.mkdir()
        original = '{"extensions": {"pinned_extensions": []}, "homepage": "https://keep.test/"}'
        _preferences(session).parent.mkdir(parents=True)
        _preferences(session).write_text(original)
        real_stat = os.stat

        def failing_stat(path: Any, *args: Any, **kwargs: Any):
            if kwargs.get("dir_fd") is not None and path == "Preferences":
                raise OSError(errno.EACCES, "synthetic verify failure")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", failing_stat)

        with pytest.raises(onepassword_setup.SessionSetupError, match="synthetic verify failure"):
            onepassword_setup.seed_pin(session)

        assert _preferences(session).read_text() == original

    def test_a_redirected_root_keeps_a_plain_seed_skip(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-root"
        outside.mkdir()
        session = tmp_path / "session"
        session.symlink_to(outside)

        onepassword_setup.seed_pin(session)

        assert list(outside.iterdir()) == []

    def test_a_plain_file_default_keeps_a_plain_seed_skip(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        session.mkdir()
        (session / "Default").write_text("not a directory")

        onepassword_setup.seed_pin(session)

        assert (session / "Default").read_text() == "not a directory"
        assert list(session.iterdir()) == [session / "Default"]


class TestNewProfilePolicy:
    def test_a_provisioned_new_profile_gets_the_pin_default(
        self,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: dict[str, object],
    ) -> None:
        instance = lifecycle.launch(
            headless=True,
            port=9471,
            profile="work",
            registry_path=registry_path,
        )

        assert instance.profile == "work"
        preferences = json.loads(_preferences(profiles_root / "work").read_text())
        assert preferences["extensions"] == {"pinned_extensions": [PINNED_ID]}
        # The reviewed password-manager updates still land beside the pin.
        assert preferences["credentials_enable_service"] is False
        assert preferences["profile"]["password_manager_enabled"] is False
        assert (profiles_root / "work" / HOSTS / DEST_NAME).is_file()

    def test_an_existing_profile_keeps_its_pin_choice_without_provisioning(
        self,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: dict[str, object],
    ) -> None:
        original = {
            "extensions": {"pinned_extensions": []},
            "homepage": "https://example.test/",
            "browser": {"show_home_button": True},
        }
        profile = profiles_root / "work"
        _preferences(profile).parent.mkdir(parents=True)
        _preferences(profile).write_text(json.dumps(original))

        lifecycle.launch(headless=True, port=9472, profile="work", registry_path=registry_path)

        # The pin choice and the unrelated settings survive; only the reviewed
        # password-manager updates core always performs are added.
        preferences = json.loads(_preferences(profile).read_text())
        assert preferences["extensions"] == {"pinned_extensions": []}
        assert preferences["homepage"] == "https://example.test/"
        assert preferences["browser"] == {"show_home_button": True}
        assert preferences["credentials_enable_service"] is False
        assert preferences["profile"]["password_manager_enabled"] is False
        assert not (profile / HOSTS).exists()

    def test_an_explicit_flag_revalidation_seeds_only_the_missing_pin(
        self,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: dict[str, object],
    ) -> None:
        profile = profiles_root / "work"
        (profile / "Default").mkdir(parents=True)

        lifecycle.launch(
            headless=True,
            port=9473,
            profile="work",
            onepassword=True,
            registry_path=registry_path,
        )

        preferences = json.loads(_preferences(profile).read_text())
        assert preferences["extensions"] == {"pinned_extensions": [PINNED_ID]}
        assert (profile / HOSTS / DEST_NAME).is_file()


_NAMED_SEED_BOUNDARIES = {
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


class TestNamedSeedBoundaries:
    """A Named Profile whose seed cannot be vouched for aborts closed.

    The real provisioner and the real core launcher run; only the seed's own
    filesystem boundary fails, synthetically. The abort happens before any
    spawn, the Named directory and everything already in it - registration,
    existing documents, symlinks - are retained untouched, and the error
    carries the remedy a named profile can actually use.
    """

    @pytest.mark.parametrize("boundary", sorted(_NAMED_SEED_BOUNDARIES))
    def test_a_seed_boundary_failure_retains_the_named_profile(
        self,
        monkeypatch: pytest.MonkeyPatch,
        boundary: str,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: dict[str, object],
    ) -> None:
        _NAMED_SEED_BOUNDARIES[boundary](monkeypatch)

        # The setup module owns its error below the lifecycle layer; the
        # launch boundary translates it to a LifecycleError (CLI exit 1)
        # keeping the path-only reason and the retry advice verbatim.
        with pytest.raises(lifecycle.LifecycleError, match=r"synthetic .* failure") as error:
            lifecycle.launch(
                headless=True,
                port=9477,
                profile="work",
                registry_path=registry_path,
            )

        message = str(error.value)
        assert message.startswith("Launch failed: ")
        assert str(profiles_root / "work") in message
        assert "Relaunch with --1password to retry the setup." in message
        # Provisioning really ran (its registration survives), nothing was
        # spawned or re-prepared, and the Named data is retained in place.
        assert (profiles_root / "work" / HOSTS / DEST_NAME).is_file()
        assert stubbed_core_boundaries["order"] == ["cleanup"]
        assert (profiles_root / "work").is_dir()

    def test_a_seed_boundary_failure_keeps_an_existing_document(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: dict[str, object],
    ) -> None:
        profile = profiles_root / "work"
        original = '{"extensions": {"pinned_extensions": []}, "homepage": "https://keep.test/"}'
        _preferences(profile).parent.mkdir(parents=True)
        _preferences(profile).write_text(original)
        fail_pin_stat(monkeypatch, OSError(errno.EACCES, "synthetic verify failure"))

        # The same translation at the launch boundary: the setup-owned error
        # becomes a LifecycleError with the path-only reason kept verbatim.
        with pytest.raises(lifecycle.LifecycleError, match="synthetic verify failure"):
            lifecycle.launch(
                headless=True,
                port=9478,
                profile="work",
                onepassword=True,
                registry_path=registry_path,
            )

        assert _preferences(profile).read_text() == original
        assert stubbed_core_boundaries["order"] == ["cleanup"]

    def test_a_symlinked_default_keeps_a_plain_named_launch(
        self,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        synthetic_chrome_binary: Path,
        stubbed_core_boundaries: dict[str, object],
    ) -> None:
        """A redirected Default is the safe skip: no seed, plain launch.

        The seed never follows the symlink and never writes through it; the
        launch itself continues, because this witness redirects Default to a
        real directory the rest of the launch can still use.
        """
        profile = profiles_root / "work"
        redirected = profiles_root / "redirected-default"
        redirected.mkdir(parents=True)
        profile.mkdir(parents=True)
        (profile / "Default").symlink_to(redirected)

        lifecycle.launch(
            headless=True,
            port=9479,
            profile="work",
            onepassword=True,
            registry_path=registry_path,
        )

        # No pin was seeded through the redirect: what landed there is core's
        # own reviewed preference document, with no extensions choice in it.
        written = list(redirected.iterdir())
        assert written == [redirected / "Preferences"]
        assert "extensions" not in json.loads((redirected / "Preferences").read_text())
        # The registration still landed in the real profile directory.
        assert (profile / HOSTS / DEST_NAME).is_file()
        assert stubbed_core_boundaries["order"] == ["cleanup", "preferences", "spawn"]
