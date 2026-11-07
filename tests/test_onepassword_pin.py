"""The new-profile pin default: seed a missing Preferences, touch nothing else.

Seeding writes one static JSON document, ``extensions.pinned_extensions``
naming the stable 1Password extension, into a ``Default/Preferences`` file
that does not exist yet. Any entry already at that name - a document with a
pin choice, an empty list, a symlink - is left byte-for-byte alone, and no
symlink on the path is followed. The policy tests run the vendored launcher
for real through the shared stubbed-boundary fixtures (#201).
"""

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING

import pytest
from onepassword_lifecycle_fixtures import DEST_NAME, HOSTS
from onepassword_lifecycle_fixtures import _darwin as _darwin
from onepassword_lifecycle_fixtures import (
    _never_read_the_real_registration as _never_read_the_real_registration,
)
from onepassword_lifecycle_fixtures import _no_real_camoufox_runner as _no_real_camoufox_runner
from onepassword_lifecycle_fixtures import _no_real_codesign as _no_real_codesign
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
    from pathlib import Path

PINNED_ID = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"

PIN_DOCUMENT = {"extensions": {"pinned_extensions": [PINNED_ID]}}


def _preferences(profile: Path) -> Path:
    return profile / "Default" / "Preferences"


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
        real_fsync = os.fsync

        def failing_fsync(fd):
            raise OSError(5, "synthetic seed I/O failure")

        monkeypatch.setattr(os, "fsync", failing_fsync)

        with pytest.raises(onepassword_setup.SessionSetupError, match="synthetic seed I/O"):
            onepassword_setup.seed_pin(session)

        assert not _preferences(session).exists()
        assert list((_preferences(session).parent).iterdir()) == []


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
