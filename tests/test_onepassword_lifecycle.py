"""Whole-lifecycle and CLI coverage for ``launch --profile NAME --1password`` (#201).

Everything here is mocked below the seams that matter: the vendored launcher
is faked (no browser starts), codesign is replaced at the subprocess boundary,
and the official registration lives in a tmp tree. What these tests prove is
the ordering the spec pins -- eligibility before any write, holder refusal
before provisioning, provisioning under the profile lock and before Chrome --
plus the CLI flag's plumbing and exit codes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from browser_tools import cli, lifecycle, onepassword
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError, ProfileLockBusy

STABLE = onepassword.STABLE_EXTENSION_ORIGIN
OTHER_ORIGIN = "chrome-extension://" + "b" * 32 + "/"
HOSTS = "NativeMessagingHosts"
DEST_NAME = "com.1password.1password.json"
LAUNCH_JSON_KEYS = {
    "name",
    "port",
    "pid",
    "engine",
    "profile",
    "browser_version",
    "user_data_dir",
}


@pytest.fixture(autouse=True)
def _never_read_the_real_registration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        onepassword,
        "SOURCE_MANIFEST",
        tmp_path / "absent" / HOSTS / DEST_NAME,
    )


@pytest.fixture(autouse=True)
def _no_real_codesign(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(onepassword, "subprocess", types.SimpleNamespace(run=fake_run))


@pytest.fixture(autouse=True)
def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the platform so the positive paths run anywhere the suite runs."""
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture
def registry_path(tmp_path: Path) -> str:
    return str(tmp_path / "registry.json")


@pytest.fixture
def profiles_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "profiles"
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root))
    return root


@pytest.fixture
def valid_installation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A valid official registration the provisioning path can read."""
    helper = tmp_path / "app" / "1Password-BrowserSupport"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    hosts = tmp_path / "official-chrome" / HOSTS
    hosts.mkdir(parents=True, exist_ok=True)
    (hosts / DEST_NAME).write_text(
        json.dumps(
            {
                "name": "com.1password.1password",
                "description": "1Password native messaging host",
                "path": str(helper),
                "type": "stdio",
                "allowed_origins": [STABLE, OTHER_ORIGIN],
            }
        )
    )
    monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", hosts / DEST_NAME)
    return helper


@pytest.fixture
def fake_browser_launch(monkeypatch: pytest.MonkeyPatch, registry_path: str) -> dict[str, object]:
    """Replace the vendored launcher; record what it was handed."""
    captured: dict[str, object] = {}

    async def fake_launch_browser(**kwargs: object) -> object:
        captured.update(kwargs)
        info = core_registry.register(
            working_dir=str(kwargs.get("working_dir")),
            pid=4321,
            browser_version="Chrome/9",
            user_data_dir=str(kwargs.get("user_data_dir")),
            port_override=9450,
            registry_path=str(kwargs.get("registry_path")),
            pid_start="tok",
        )
        return core_registry.lookup(info.name, registry_path=str(kwargs.get("registry_path")))

    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", fake_launch_browser)
    return captured


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
        self, monkeypatch, registry_path, kwargs, fragment
    ):
        if fragment == "macOS only":
            monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(LifecycleError, match=fragment):
            lifecycle.launch(registry_path=registry_path, onepassword=True, **kwargs)
        # Refused before filesystem work: nothing appeared under the root.
        assert not any(lifecycle.profiles_root().glob("*"))

    def test_camoufox_never_spawns_its_runner(self, monkeypatch, registry_path):
        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("the camoufox runner spawned for a refused flag")

        monkeypatch.setattr(lifecycle, "_spawn_camoufox_process", boom)
        with pytest.raises(LifecycleError, match="requires the chrome engine"):
            lifecycle.launch(
                engine="camoufox", profile="work", onepassword=True, registry_path=registry_path
            )


# ---------------------------------------------------------------------------
# Ordering at the launch seam
# ---------------------------------------------------------------------------


class TestLaunchSeamOrdering:
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

    def test_opt_out_never_calls_provisioning(
        self, monkeypatch, registry_path, profiles_root, fake_browser_launch
    ):
        calls: list[Path] = []

        def never(user_data_dir: Path) -> None:
            calls.append(user_data_dir)

        monkeypatch.setattr(onepassword, "provision", never)
        instance = lifecycle.launch(
            engine="chrome", profile="work", registry_path=registry_path
        )
        assert calls == []
        assert instance.profile == "work"
        assert not (profiles_root / "work" / HOSTS).exists()

    def test_holder_refusal_precedes_provisioning(
        self, monkeypatch, registry_path, profiles_root
    ):
        calls: list[Path] = []

        def never(user_data_dir: Path) -> None:
            calls.append(user_data_dir)

        monkeypatch.setattr(onepassword, "provision", never)
        monkeypatch.setattr(
            lifecycle, "find_profile_holder", lambda *a, **k: "web-01"
        )
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
# The CLI surface: flag, forwarding, exit codes
# ---------------------------------------------------------------------------


class TestCliSurface:
    def test_the_flag_parses_and_defaults_off(self):
        assert cli.build_parser().parse_args(["launch"]).onepassword is False
        assert cli.build_parser().parse_args(["launch", "--1password"]).onepassword is True

    def test_the_flag_is_forwarded_and_the_json_stays_unchanged(
        self, monkeypatch, capsys, tmp_path, registry_path
    ):
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
    def test_unsupported_combinations_are_operational_errors(
        self, monkeypatch, capsys, tmp_path, registry_path, profiles_root, argv, fragment
    ):
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, registry_path)
        assert cli.main(argv) == cli.EXIT_OPERATIONAL
        assert fragment in capsys.readouterr().err
        # Nothing was written on the way out.
        assert list(profiles_root.glob("*")) == []

    def test_a_provisioning_failure_is_an_operational_error_before_any_browser(
        self, monkeypatch, capsys, registry_path, profiles_root, fake_browser_launch
    ):
        def refused(user_data_dir: Path) -> None:
            raise onepassword.OnePasswordProvisionError("no official registration")

        monkeypatch.setattr(onepassword, "provision", refused)
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, registry_path)
        assert cli.main(["launch", "--profile", "work", "--1password"]) == cli.EXIT_OPERATIONAL
        assert "no official registration" in capsys.readouterr().err
        assert fake_browser_launch == {}
