"""Whole-lifecycle coverage for ``launch --profile NAME --1password`` (#201).

Everything here is mocked below the seams that matter: the vendored launcher
is faked (no browser starts), codesign is replaced at the subprocess boundary,
and the official registration lives in a tmp tree. What these tests prove is
the ordering the spec pins -- eligibility before any write, holder refusal
before provisioning, provisioning under the profile lock and before Chrome.
A brand-new Named Chrome profile on macOS is also registered automatically
when the official source exists: that default's coverage lives in
TestAutomaticRegistration. The CLI flag's plumbing and exit codes are in
``test_onepassword_cli.py``; the shared fixtures live in
``onepassword_lifecycle_fixtures.py``.
"""

from __future__ import annotations

import errno
import json
import subprocess
import sys
import types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from onepassword_lifecycle_fixtures import DEST_NAME, HOSTS, STABLE
from onepassword_lifecycle_fixtures import _darwin as _darwin
from onepassword_lifecycle_fixtures import (
    _never_read_the_real_registration as _never_read_the_real_registration,
)
from onepassword_lifecycle_fixtures import _no_real_camoufox_runner as _no_real_camoufox_runner
from onepassword_lifecycle_fixtures import _no_real_codesign as _no_real_codesign
from onepassword_lifecycle_fixtures import fake_browser_launch as fake_browser_launch
from onepassword_lifecycle_fixtures import profiles_root as profiles_root
from onepassword_lifecycle_fixtures import registry_path as registry_path
from onepassword_lifecycle_fixtures import valid_installation as valid_installation

from browser_tools import lifecycle, onepassword
from browser_tools.lifecycle import LifecycleError, ProfileLockBusy
from browser_tools.usage import UsageError

# ---------------------------------------------------------------------------
# Eligibility: refused before any filesystem work
# ---------------------------------------------------------------------------


class TestEligibilityBeforeWrites:
    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"engine": "chrome", "profile": "work"}, "macOS only"),
            ({"engine": "camoufox", "profile": "work"}, "requires the chrome engine"),
            ({"engine": "chrome", "profile": None}, "provisions a named profile"),
        ],
    )
    def test_unsupported_combinations_are_refused(
        self, monkeypatch, registry_path, profiles_root, fake_browser_launch, kwargs, fragment
    ):
        if fragment == "macOS only":
            monkeypatch.setattr(sys, "platform", "linux")
        error = LifecycleError if fragment == "macOS only" else UsageError
        with pytest.raises(error, match=fragment):
            lifecycle.launch(registry_path=registry_path, onepassword=True, **kwargs)
        assert fake_browser_launch == {}
        # Refused before filesystem work: nothing appeared under the root.
        assert not any(lifecycle.profiles_root().glob("*"))

    @pytest.mark.parametrize("engine,profile", [("camoufox", "work"), ("chrome", None)])
    def test_invalid_combinations_are_usage_errors_on_linux(
        self, monkeypatch, registry_path, profiles_root, fake_browser_launch, engine, profile
    ):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(UsageError):
            lifecycle.launch(
                engine=engine, profile=profile, onepassword=True, registry_path=registry_path
            )
        assert fake_browser_launch == {}
        assert not any(profiles_root.glob("*"))

    def test_camoufox_never_spawns_its_runner(self, monkeypatch, registry_path):
        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("the camoufox runner spawned for a refused flag")

        monkeypatch.setattr(lifecycle, "_spawn_camoufox_process", boom)
        with pytest.raises(UsageError, match="requires the chrome engine"):
            lifecycle.launch(
                engine="camoufox", profile="work", onepassword=True, registry_path=registry_path
            )


# ---------------------------------------------------------------------------
# Ordering at the launch seam
# ---------------------------------------------------------------------------


class TestLaunchSeamOrdering:
    def test_existing_profile_acquires_registration_without_losing_login(
        self, registry_path, profiles_root, valid_installation, fake_browser_launch
    ):
        profile = profiles_root / "work"
        login = profile / "Default" / "Login Data"
        login.parent.mkdir(parents=True)
        login.write_bytes(b"existing login sentinel")

        instance = lifecycle.launch(profile="work", onepassword=True, registry_path=registry_path)

        assert instance.profile == "work"
        assert fake_browser_launch["user_data_dir"] == str(profile)
        manifest = json.loads((profile / HOSTS / DEST_NAME).read_text())
        assert manifest["allowed_origins"] == [STABLE]
        assert manifest["path"] == str(valid_installation)
        assert login.read_bytes() == b"existing login sentinel"

    def test_symlinked_profile_is_refused_before_singleton_cleanup(
        self, monkeypatch, registry_path, profiles_root, tmp_path, fake_browser_launch
    ):
        external = tmp_path / "external"
        external.mkdir()
        singleton = external / "SingletonCookie"
        singleton.write_text("external browser state")
        profiles_root.mkdir()
        (profiles_root / "work").symlink_to(external)

        def forbidden_cleanup(*args, **kwargs):
            raise AssertionError("cleanup reached an external profile")

        monkeypatch.setattr(lifecycle, "clean_stale_singleton_lock", forbidden_cleanup)
        with pytest.raises(LifecycleError, match="not the symlink"):
            lifecycle.launch(profile="work", onepassword=True, registry_path=registry_path)
        assert singleton.read_text() == "external browser state"
        assert fake_browser_launch == {}
        assert not (external / HOSTS).exists()

    def test_absent_official_source_never_provisions(
        self, monkeypatch, registry_path, profiles_root, fake_browser_launch
    ):
        # The autouse fixture keeps SOURCE_MANIFEST absent, so a launch with no
        # flag has no official source to register from.
        calls: list[Path] = []

        def never(user_data_dir: Path) -> None:
            calls.append(user_data_dir)

        monkeypatch.setattr(onepassword, "provision", never)
        instance = lifecycle.launch(engine="chrome", profile="work", registry_path=registry_path)
        assert calls == []
        assert instance.profile == "work"
        assert not (profiles_root / "work" / HOSTS).exists()

    def test_holder_refusal_precedes_provisioning(self, monkeypatch, registry_path, profiles_root):
        calls: list[Path] = []

        def never(user_data_dir: Path) -> None:
            calls.append(user_data_dir)

        monkeypatch.setattr(onepassword, "provision", never)
        monkeypatch.setattr(lifecycle, "find_profile_holder", lambda *a, **k: "web-01")
        with pytest.raises(LifecycleError, match="already held by live instance 'web-01'"):
            lifecycle.launch(
                engine="chrome", profile="work", onepassword=True, registry_path=registry_path
            )
        assert calls == []
        assert not (profiles_root / "work" / HOSTS).exists()

    def test_provisioning_failure_precedes_browser_launch(
        self, monkeypatch, registry_path, profiles_root, fake_browser_launch
    ):
        def refused(user_data_dir: Path) -> None:
            raise onepassword.OnePasswordProvisionError("helper failed verification")

        monkeypatch.setattr(onepassword, "provision", refused)
        with pytest.raises(LifecycleError, match="helper failed verification"):
            lifecycle.launch(
                engine="chrome", profile="work", onepassword=True, registry_path=registry_path
            )
        assert fake_browser_launch == {}

    def test_provisioning_runs_under_the_profile_launch_lock(
        self, monkeypatch, registry_path, profiles_root, fake_browser_launch
    ):
        """Provisioning happens while the profile lock is held: a second
        acquisition attempt from inside it must find the lock busy."""
        observed: list[str] = []

        def probe(user_data_dir: Path) -> None:
            try:
                with lifecycle.profile_launch_lock("work", timeout=0.05):
                    observed.append("acquired")
            except ProfileLockBusy:
                observed.append("busy")

        monkeypatch.setattr(onepassword, "provision", probe)
        lifecycle.launch(
            engine="chrome", profile="work", onepassword=True, registry_path=registry_path
        )
        assert observed == ["busy"]

    def test_provisioning_targets_the_active_profile_directory(
        self, registry_path, profiles_root, valid_installation, fake_browser_launch
    ):
        instance = lifecycle.launch(
            engine="chrome",
            profile="work",
            name="shop",
            onepassword=True,
            registry_path=registry_path,
        )
        manifest = profiles_root / "work" / HOSTS / DEST_NAME
        written = json.loads(manifest.read_text())
        assert written == {
            "name": "com.1password.1password",
            "description": "1Password native messaging host",
            "path": str(valid_installation),
            "type": "stdio",
            "allowed_origins": [STABLE],
        }
        # The launch itself is unchanged: registered, extended fields written.
        assert instance.profile == "work"
        assert fake_browser_launch["user_data_dir"] == str(profiles_root / "work")


# ---------------------------------------------------------------------------
# Automatic registration of brand-new Named Profiles
# ---------------------------------------------------------------------------


class TestAutomaticRegistration:
    """A fresh macOS Named Chrome profile registers itself; nothing else does.

    The default is user-selected: no flag, no new seam. Everything the
    explicit path checks stays inside the provisioner; these tests pin only
    which launches reach it.
    """

    def test_fresh_profile_registers_automatically(
        self,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        assert fake_browser_launch["user_data_dir"] == str(profiles_root / "work")
        manifest_path = profiles_root / "work" / HOSTS / DEST_NAME
        written = json.loads(manifest_path.read_text())
        assert written == {
            "name": "com.1password.1password",
            "description": "1Password native messaging host",
            "path": str(valid_installation),
            "type": "stdio",
            "allowed_origins": [STABLE],
        }
        assert manifest_path.stat().st_mode & 0o777 == 0o600

    def test_existing_profile_stays_unregistered_without_the_flag(
        self,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        profile = profiles_root / "work"
        login = profile / "Default" / "Login Data"
        login.parent.mkdir(parents=True)
        login.write_bytes(b"existing login sentinel")

        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        assert fake_browser_launch["user_data_dir"] == str(profile)
        assert not (profile / HOSTS).exists()
        assert login.read_bytes() == b"existing login sentinel"

    def test_absent_source_keeps_a_plain_launch(
        self, registry_path: str, profiles_root: Path, fake_browser_launch: dict[str, object]
    ) -> None:
        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        assert fake_browser_launch  # the browser launched, nothing else happened
        assert not (profiles_root / "work" / HOSTS).exists()

    def test_unsupported_platform_keeps_a_plain_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        assert fake_browser_launch
        assert not (profiles_root / "work" / HOSTS).exists()

    def test_camoufox_profile_keeps_a_plain_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
    ) -> None:
        captured: dict[str, object] = {}

        def fake_camoufox(**kwargs: object) -> lifecycle.ExtendedInstance:
            captured.update(kwargs)
            return lifecycle.ExtendedInstance(
                name="work-01",
                port=9450,
                pid=512,
                browser_version="camoufox",
                user_data_dir=str(profiles_root / "work"),
                launched=None,
                pid_start=None,
                engine="camoufox",
                profile="work",
            )

        monkeypatch.setattr(lifecycle, "_launch_camoufox", fake_camoufox)
        instance = lifecycle.launch(engine="camoufox", profile="work", registry_path=registry_path)

        assert instance.engine == "camoufox"
        assert captured["user_data_dir"] == profiles_root / "work"
        assert not (profiles_root / "work" / HOSTS).exists()

    def test_unnamed_launch_never_provisions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        calls: list[Path] = []

        def never(user_data_dir: Path) -> None:
            calls.append(user_data_dir)

        monkeypatch.setattr(onepassword, "provision", never)
        lifecycle.launch(registry_path=registry_path)

        assert calls == []
        assert fake_browser_launch

    def test_migrated_legacy_profile_is_not_new(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        legacy = profiles_root.parent / "legacy"
        monkeypatch.setenv(lifecycle.LEGACY_PROFILES_ENV_VAR, str(legacy))
        source = legacy / "work"
        login = source / "Default" / "Login Data"
        login.parent.mkdir(parents=True)
        login.write_bytes(b"legacy login sentinel")

        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        assert not (profiles_root / "work" / HOSTS).exists()
        assert (profiles_root / "work" / "Default" / "Login Data").read_bytes() == (
            b"legacy login sentinel"
        )
        assert not source.exists()

    def test_invalid_present_source_refuses_before_browser_spawn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        profiles_root: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        invalid = tmp_path / HOSTS / DEST_NAME
        invalid.parent.mkdir(parents=True)
        invalid.write_text("{ not json")
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", invalid)

        with pytest.raises(LifecycleError, match="not valid JSON") as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        assert fake_browser_launch == {}
        # The launch carried no flag, so the refusal cannot offer one.
        assert "--1password" not in str(error.value)

    def test_dangling_source_link_is_invalid_rather_than_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """A dangling symlink at the official source path is a present,
        invalid entry (adversarial A2): it must reach the provisioner's
        refusal instead of being classified as absent and launched around.
        """
        source = tmp_path / HOSTS / DEST_NAME
        source.parent.mkdir(parents=True)
        source.symlink_to(tmp_path / "absent-target.json")
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", source)

        with pytest.raises(LifecycleError, match="could not be read as a regular file"):
            lifecycle.launch(profile="work", registry_path=registry_path)

        assert fake_browser_launch == {}

    def test_retry_after_failed_initialization_still_refuses_unchanged_invalid_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """A failed automatic initialization must not silently disable the
        gate (adversarial A1): with the source still invalid, the retry is
        refused again with the failure visible.
        """
        source = onepassword.source_manifest_path()
        source.write_text("{ not json")
        with pytest.raises(LifecycleError, match="not valid JSON"):
            lifecycle.launch(profile="work", registry_path=registry_path)
        assert fake_browser_launch == {}

        with pytest.raises(LifecycleError, match="not valid JSON"):
            lifecycle.launch(profile="work", registry_path=registry_path)

        assert fake_browser_launch == {}

    def test_retry_after_failed_initialization_registers_once_the_source_is_repaired(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """A repaired official source must produce the promised automatic
        registration on retry, not a permanently-plain existing profile.
        """
        source = onepassword.source_manifest_path()
        valid_source = source.read_bytes()
        source.write_text("{ not json")
        with pytest.raises(LifecycleError, match="not valid JSON"):
            lifecycle.launch(profile="work", registry_path=registry_path)

        source.write_bytes(valid_source)
        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        assert fake_browser_launch["user_data_dir"] == str(profiles_root / "work")
        manifest = json.loads((profiles_root / "work" / HOSTS / DEST_NAME).read_text())
        assert manifest["allowed_origins"] == [STABLE]
        assert manifest["path"] == str(valid_installation)

    def test_an_interrupted_validation_discards_the_new_directory_and_the_retry_registers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """Ctrl-C during the helper check must not turn the next launch plain.

        The interruption propagates unchanged (the very same exception
        object), the empty directory this launch created is gone, and the
        retry still registers the profile automatically.
        """
        interrupt = KeyboardInterrupt()
        real_validate = onepassword._validate_helper
        state = {"interrupt": True}

        def flaky_validate(helper_path: str) -> None:
            if state["interrupt"]:
                raise interrupt
            real_validate(helper_path)

        monkeypatch.setattr(onepassword, "_validate_helper", flaky_validate)

        with pytest.raises(KeyboardInterrupt) as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        assert error.value is interrupt
        assert not (profiles_root / "work").exists()
        assert fake_browser_launch == {}

        state["interrupt"] = False
        lifecycle.launch(profile="work", registry_path=registry_path)

        manifest = json.loads((profiles_root / "work" / HOSTS / DEST_NAME).read_text())
        assert manifest["allowed_origins"] == [STABLE]
        assert fake_browser_launch["user_data_dir"] == str(profiles_root / "work")

    def test_a_live_holder_refusal_discards_the_new_directory_and_the_retry_registers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """A refusal before provisioning leaves no directory behind either.

        The profile is still new on the retry, so once the holder is gone the
        launch registers it instead of treating it as an existing profile.
        """

        def held(profile: str, *, registry_path: str | None = None) -> str | None:
            return "other-01"

        def free(profile: str, *, registry_path: str | None = None) -> str | None:
            return None

        monkeypatch.setattr(lifecycle, "find_profile_holder", held)

        with pytest.raises(LifecycleError, match="already held by live instance 'other-01'"):
            lifecycle.launch(profile="work", registry_path=registry_path)

        assert not (profiles_root / "work").exists()
        assert fake_browser_launch == {}

        monkeypatch.setattr(lifecycle, "find_profile_holder", free)
        lifecycle.launch(profile="work", registry_path=registry_path)

        assert (profiles_root / "work" / HOSTS / DEST_NAME).is_file()
        assert fake_browser_launch["user_data_dir"] == str(profiles_root / "work")

    def test_an_interruption_never_deletes_data_the_launch_did_not_leave_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """Only empty directories are removed; anything inside is retained."""
        sentinel = profiles_root / "work" / "Default" / "sentinel.txt"
        interrupt = KeyboardInterrupt()

        def interrupt_after_data_appears(user_data_dir: Path) -> None:
            sentinel.parent.mkdir(parents=True)
            sentinel.write_bytes(b"synthetic sentinel")
            raise interrupt

        monkeypatch.setattr(lifecycle, "clean_stale_singleton_lock", interrupt_after_data_appears)

        with pytest.raises(KeyboardInterrupt) as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        assert error.value is interrupt
        assert sentinel.read_bytes() == b"synthetic sentinel"
        assert fake_browser_launch == {}

    def test_failed_publication_retry_can_register_while_the_profile_is_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """An injected pre-publication filesystem failure cleans up the empty
        initialization it created, so the same launch can be retried.
        """
        failing = {"on": True}
        real_write = onepassword._write_manifest

        def flaky_write(hosts_fd: int, filename: Path, canonical: dict) -> None:
            if failing["on"]:
                raise OSError(errno.EIO, "synthetic publication failure")
            real_write(hosts_fd, filename, canonical)

        monkeypatch.setattr(onepassword, "_write_manifest", flaky_write)
        with pytest.raises(LifecycleError, match="synthetic publication failure"):
            lifecycle.launch(profile="work", registry_path=registry_path)
        assert fake_browser_launch == {}
        # Only the empty initialization was cleaned up; the profile is new again.
        assert not (profiles_root / "work").exists()

        failing["on"] = False
        instance = lifecycle.launch(profile="work", registry_path=registry_path)

        assert instance.profile == "work"
        manifest = json.loads((profiles_root / "work" / HOSTS / DEST_NAME).read_text())
        assert manifest["path"] == str(valid_installation)

    def test_failed_initialization_preserves_nonempty_profile_data(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """Unexpected data in the new profile directory is never deleted;
        that exceptional failure points at the explicit flag for recovery,
        because the kept directory makes a plain retry an existing profile.
        """

        def writes_then_fails(user_data_dir: Path) -> None:
            login = user_data_dir / "Default" / "Login Data"
            login.parent.mkdir(parents=True)
            login.write_bytes(b"unexpected synthetic data")
            raise onepassword.OnePasswordProvisionError("helper failed verification")

        monkeypatch.setattr(onepassword, "provision", writes_then_fails)
        with pytest.raises(LifecycleError) as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        sentinel = profiles_root / "work" / "Default" / "Login Data"
        assert sentinel.read_bytes() == b"unexpected synthetic data"
        assert fake_browser_launch == {}
        message = str(error.value)
        assert "helper failed verification" in message
        assert "--1password" in message
        assert "Launch without" not in message

    def test_automatic_source_failure_advice_never_advises_removal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        registry_path: str,
        profiles_root: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """The automatic refusal keeps the provisioner's reason plus generic
        retry advice; it never suggests removing the everyday source
        registration and never repeats the explicit flag's suffix.
        """
        invalid = tmp_path / HOSTS / DEST_NAME
        invalid.parent.mkdir(parents=True)
        invalid.write_text("{ not json")
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", invalid)

        with pytest.raises(LifecycleError, match="not valid JSON") as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        message = str(error.value)
        assert fake_browser_launch == {}
        assert not (profiles_root / "work").exists()
        assert "--1password" not in message
        assert "Launch without" not in message
        assert "remove" not in message.lower()
        assert "everyday" not in message

    @pytest.mark.parametrize("failure", ["signing", "unexecutable"])
    def test_automatic_helper_failure_names_the_helper_not_a_registration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
        failure: str,
    ) -> None:
        """A helper failure is about the helper executable; the automatic
        advice must not relabel that path as a registration to remove.
        """
        if failure == "signing":

            def rejected(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                return subprocess.CompletedProcess(args, 1, b"", b"")

            monkeypatch.setattr(onepassword, "subprocess", types.SimpleNamespace(run=rejected))
            refused_with = "code-signature verification"
        else:
            valid_installation.chmod(0o644)
            refused_with = "is not an executable file"

        with pytest.raises(LifecycleError, match=refused_with) as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        message = str(error.value)
        assert fake_browser_launch == {}
        if failure == "unexecutable":
            assert str(valid_installation) in message
        assert "registration" not in message
        assert "remove" not in message.lower()
        assert "everyday" not in message
        assert "Launch without" not in message

    def test_automatic_destination_failure_keeps_the_conflicting_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        registry_path: str,
        profiles_root: Path,
        valid_installation: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        """A conflicting registration that appears after the newness check is
        neither replaced nor deleted; the automatic refusal keeps the file and
        the only removal it names is that profile's own conflicting file.
        """
        planted = {
            "name": "com.1password.1password",
            "description": "conflicting synthetic registration",
            "path": "/usr/bin/true",
            "type": "stdio",
            "allowed_origins": [STABLE],
        }
        real_clean = lifecycle.clean_stale_singleton_lock

        def clean_and_plant_conflict(directory: Path) -> None:
            real_clean(directory)
            hosts = directory / HOSTS
            hosts.mkdir(exist_ok=True)
            (hosts / DEST_NAME).write_text(json.dumps(planted))

        monkeypatch.setattr(lifecycle, "clean_stale_singleton_lock", clean_and_plant_conflict)
        with pytest.raises(LifecycleError, match="holds a conflicting registration") as error:
            lifecycle.launch(profile="work", registry_path=registry_path)

        message = str(error.value)
        destination = profiles_root / "work" / HOSTS / DEST_NAME
        assert json.loads(destination.read_text()) == planted
        assert fake_browser_launch == {}
        assert str(destination) in message
        assert "--1password" in message
        assert "Launch without" not in message
        assert "everyday" not in message
        # The single removal the message names is the profile's own
        # conflicting file, never the everyday Chrome source.
        assert message.lower().count("remove") == 1
        assert "or remove it yourself to re-provision" in message
