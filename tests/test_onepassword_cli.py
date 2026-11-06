"""CLI coverage for ``launch --profile NAME --1password`` (#201).

The flag's parsing, its forwarding into ``lifecycle.launch``, the unchanged
launch JSON, and the exit codes. Extracted from
``test_onepassword_lifecycle.py`` (repository module-size rule); the shared
fixtures come from ``onepassword_lifecycle_fixtures.py``, imported
explicitly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# The fixtures are re-exported under their own names (the explicit `x as x`
# alias is pyflakes' re-export form), so pytest resolves them from this module
# while lint stays clean. The autouse guards keep the CLI tests off the real
# registration, codesign and Camoufox runner.
from onepassword_lifecycle_fixtures import _darwin as _darwin
from onepassword_lifecycle_fixtures import (
    _never_read_the_real_registration as _never_read_the_real_registration,
)
from onepassword_lifecycle_fixtures import _no_real_camoufox_runner as _no_real_camoufox_runner
from onepassword_lifecycle_fixtures import _no_real_codesign as _no_real_codesign
from onepassword_lifecycle_fixtures import fake_browser_launch as fake_browser_launch
from onepassword_lifecycle_fixtures import profiles_root as profiles_root
from onepassword_lifecycle_fixtures import registry_path as registry_path

from browser_tools import cli, lifecycle, onepassword

LAUNCH_JSON_KEYS = {
    "name",
    "port",
    "pid",
    "engine",
    "profile",
    "browser_version",
    "user_data_dir",
}


class TestCliSurface:
    def test_the_flag_parses_and_defaults_off(self) -> None:
        assert cli.build_parser().parse_args(["launch"]).onepassword is False
        assert cli.build_parser().parse_args(["launch", "--1password"]).onepassword is True

    def test_the_flag_is_forwarded_and_the_json_stays_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        registry_path: str,
    ) -> None:
        captured: dict[str, object] = {}

        def fake_launch(**kwargs: object) -> lifecycle.ExtendedInstance:
            captured.update(kwargs)
            return lifecycle.ExtendedInstance(
                name="work-01",
                port=9450,
                pid=4321,
                browser_version="Chrome/9",
                user_data_dir=str(tmp_path / "profiles" / "work"),
                launched=None,
                pid_start=None,
                engine="chrome",
                profile="work",
            )

        monkeypatch.setattr(lifecycle, "launch", fake_launch)
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, registry_path)

        assert cli.main(["launch", "--profile", "work", "--1password"]) == cli.EXIT_OK
        assert captured["onepassword"] is True
        document = json.loads(capsys.readouterr().out)
        assert set(document) == LAUNCH_JSON_KEYS

        assert cli.main(["launch", "--profile", "work"]) == cli.EXIT_OK
        assert captured["onepassword"] is False

    @pytest.mark.parametrize(
        ("argv", "fragment"),
        [
            (
                ["launch", "--engine", "camoufox", "--profile", "work", "--1password"],
                "--1password requires the chrome engine",
            ),
            (["launch", "--1password"], "--1password provisions a named profile"),
        ],
    )
    def test_unsupported_combinations_are_usage_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        registry_path: str,
        profiles_root: Path,
        fake_browser_launch: dict[str, object],
        argv: list[str],
        fragment: str,
    ) -> None:
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, registry_path)
        assert cli.main(argv) == cli.EXIT_USAGE
        assert fragment in capsys.readouterr().err
        # Nothing was written on the way out.
        assert list(profiles_root.glob("*")) == []

    def test_a_provisioning_failure_is_an_operational_error_before_any_browser(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        registry_path: str,
        profiles_root: Path,
        fake_browser_launch: dict[str, object],
    ) -> None:
        def refused(user_data_dir: Path) -> None:
            raise onepassword.OnePasswordProvisionError("no official registration")

        monkeypatch.setattr(onepassword, "provision", refused)
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, registry_path)
        assert cli.main(["launch", "--profile", "work", "--1password"]) == cli.EXIT_OPERATIONAL
        assert "no official registration" in capsys.readouterr().err
        assert fake_browser_launch == {}
