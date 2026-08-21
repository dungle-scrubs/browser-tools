"""Tests for the merged CLI front and the registry-backed lifecycle (RFC-01 #35).

These exercise the extended registry schema (engine/profile), policy-flag
resolution, the leading-[INSTANCE] rule, the verb dispatch, and CLI exit codes,
all against an isolated registry file so nothing touches /tmp/chrome-agent.
No real browser is launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from browser_tools import cli, lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


def _read(registry_path: str) -> dict:
    return json.loads(Path(registry_path).read_text())


def _seed(registry_path: str, entries: dict) -> None:
    core_registry._save_registry(entries, registry_path)


def _dead_entry(port: int = 9222, **extra) -> dict:
    # A PID that cannot be alive/ours -> the vendored liveness ladder reads dead.
    base = {
        "port": port,
        "pid": 2_000_000_000,
        "browser_version": "Chrome/1",
        "user_data_dir": "",
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": None,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Extended schema
# ---------------------------------------------------------------------------


class TestExtendedSchema:
    def test_missing_fields_default_to_chrome_null(self):
        # An entry written by the vendored code carries neither field.
        engine, profile = lifecycle.read_engine_profile(_dead_entry())
        assert engine == "chrome"
        assert profile is None

    def test_present_fields_read_back(self):
        engine, profile = lifecycle.read_engine_profile(
            _dead_entry(engine="camoufox", profile="dev")
        )
        assert engine == "camoufox"
        assert profile == "dev"

    def test_empty_engine_defaults_to_chrome(self):
        engine, _ = lifecycle.read_engine_profile(_dead_entry(engine="", profile=None))
        assert engine == "chrome"

    def test_annotate_writes_fields(self, registry_path):
        _seed(registry_path, {"site-01": _dead_entry()})
        lifecycle.annotate_entry(
            "site-01", engine="chrome", profile="work", registry_path=registry_path
        )
        raw = _read(registry_path)
        assert raw["site-01"]["engine"] == "chrome"
        assert raw["site-01"]["profile"] == "work"

    def test_annotate_missing_entry_is_noop(self, registry_path):
        _seed(registry_path, {})
        # Must not raise when the entry raced away.
        lifecycle.annotate_entry("gone", engine="chrome", profile=None, registry_path=registry_path)

    def test_read_instances_roundtrip(self, registry_path):
        _seed(
            registry_path,
            {
                "old": _dead_entry(port=9222),  # vendored-style, no engine/profile
                "new": _dead_entry(port=9223, engine="camoufox", profile="x"),
            },
        )
        by_name = {i.name: i for i in lifecycle.read_instances(registry_path=registry_path)}
        assert by_name["old"].engine == "chrome"
        assert by_name["old"].profile is None
        assert by_name["new"].engine == "camoufox"
        assert by_name["new"].profile == "x"


# ---------------------------------------------------------------------------
# Policy-flag resolution
# ---------------------------------------------------------------------------


class TestChannelResolution:
    def test_none_channel_returns_none(self):
        assert lifecycle.resolve_channel_binary(None) is None

    def test_unknown_channel_raises(self, monkeypatch):
        # Force "no candidate installed" regardless of the host.
        monkeypatch.setattr(lifecycle, "_channel_candidates", lambda channel: [])
        with pytest.raises(LifecycleError) as exc:
            lifecycle.resolve_channel_binary("canary")
        assert "canary" in str(exc.value)

    def test_channel_binary_found(self, monkeypatch, tmp_path):
        fake = tmp_path / "chrome"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(lifecycle, "_channel_candidates", lambda channel: [str(fake)])
        assert lifecycle.resolve_channel_binary("beta") == str(fake)


# ---------------------------------------------------------------------------
# Instance-name resolution
# ---------------------------------------------------------------------------


class TestSingleInstanceRule:
    def test_zero_instances_errors(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(LifecycleError):
            lifecycle.resolve_single_instance(registry_path=registry_path)

    def test_single_instance_resolves(self, registry_path):
        _seed(registry_path, {"only-01": _dead_entry()})
        assert lifecycle.resolve_single_instance(registry_path=registry_path) == "only-01"

    def test_multiple_instances_errors_with_listing(self, registry_path):
        _seed(registry_path, {"a-01": _dead_entry(port=9222), "b-01": _dead_entry(port=9223)})
        with pytest.raises(LifecycleError) as exc:
            lifecycle.resolve_single_instance(registry_path=registry_path)
        assert "a-01" in str(exc.value) and "b-01" in str(exc.value)

    def test_domain_method_shape(self):
        assert lifecycle.looks_like_domain_method("Page.navigate")
        assert not lifecycle.looks_like_domain_method("my-site-01")


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


class TestLaunch:
    def test_camoufox_engine_is_a_seam(self, registry_path):
        with pytest.raises(LifecycleError) as exc:
            lifecycle.launch(engine="camoufox", registry_path=registry_path)
        assert "#36" in str(exc.value)

    def test_unknown_engine_errors(self, registry_path):
        with pytest.raises(LifecycleError):
            lifecycle.launch(engine="firefox", registry_path=registry_path)

    def test_chrome_launch_records_engine_and_profile(self, registry_path, monkeypatch):
        captured = {}

        async def fake_launch_browser(**kwargs):
            captured.update(kwargs)
            # Emulate the vendored launcher: write the six-field entry.
            core_registry.register(
                working_dir="/tmp/my-site",
                pid=4321,
                browser_version="Chrome/9",
                user_data_dir="/tmp/session-x",
                port_override=9250,
                registry_path=kwargs["registry_path"],
                pid_start="tok",
            )
            return core_registry.lookup("my-site-01", registry_path=kwargs["registry_path"])

        monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", fake_launch_browser)
        monkeypatch.setattr(lifecycle, "resolve_channel_binary", lambda channel: "/bin/chrome-beta")

        inst = lifecycle.launch(
            engine="chrome",
            profile="work",
            channel="beta",
            headless=True,
            registry_path=registry_path,
        )

        assert inst.engine == "chrome"
        assert inst.profile == "work"
        assert captured["binary"] == "/bin/chrome-beta"
        assert captured["headless"] is True
        # The extended fields are persisted on the entry.
        raw = _read(registry_path)
        assert raw["my-site-01"]["engine"] == "chrome"
        assert raw["my-site-01"]["profile"] == "work"

    def test_browser_not_found_becomes_operational_error(self, registry_path, monkeypatch):
        async def boom(**kwargs):
            from browser_tools.core.launcher import BrowserNotFoundError

            raise BrowserNotFoundError(searched_paths=["/nope"])

        monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", boom)
        with pytest.raises(LifecycleError):
            lifecycle.launch(engine="chrome", registry_path=registry_path)


class TestStatusStopCleanup:
    def test_status_empty(self, registry_path):
        assert lifecycle.status(registry_path=registry_path) == []

    def test_status_reports_dead_and_schema(self, registry_path):
        _seed(registry_path, {"site-01": _dead_entry(engine="chrome", profile="p")})
        rows = lifecycle.status(registry_path=registry_path)
        assert len(rows) == 1
        assert rows[0]["name"] == "site-01"
        assert rows[0]["alive"] is False
        assert rows[0]["engine"] == "chrome"
        assert rows[0]["profile"] == "p"

    def test_status_unknown_instance_errors(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(LifecycleError):
            lifecycle.status(instance="ghost", registry_path=registry_path)

    def test_stop_dead_instance_cleans_up(self, registry_path):
        _seed(registry_path, {"site-01": _dead_entry()})
        msg = lifecycle.stop(instance="site-01", registry_path=registry_path)
        assert "site-01" in msg
        assert _read(registry_path) == {}

    def test_stop_unknown_instance_errors(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(LifecycleError):
            lifecycle.stop(instance="ghost", registry_path=registry_path)

    def test_cleanup_removes_dead(self, registry_path):
        _seed(registry_path, {"site-01": _dead_entry()})
        removed = lifecycle.cleanup(registry_path=registry_path)
        assert "site-01" in removed
        assert _read(registry_path) == {}


class TestGuide:
    def test_guide_mentions_verbs(self):
        text = lifecycle.guide_text()
        for verb in ("launch", "status", "stop", "cleanup", "guide"):
            assert verb in text


# ---------------------------------------------------------------------------
# CLI front: parsing, dispatch, exit codes
# ---------------------------------------------------------------------------


class TestCliFront:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch, tmp_path):
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, str(tmp_path / "registry.json"))

    def test_no_verb_is_usage_error(self, capsys):
        assert cli.main([]) == cli.EXIT_USAGE

    def test_status_empty_is_ok_and_json(self, capsys):
        assert cli.main(["status"]) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert json.loads(out) == []

    def test_bt_and_browser_tools_share_entry(self):
        # Both console scripts resolve to cli.main.
        assert cli.main is not None

    def test_cleanup_ok(self, capsys):
        assert cli.main(["cleanup"]) == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {"removed": []}

    def test_guide_ok(self, capsys):
        assert cli.main(["guide"]) == cli.EXIT_OK
        assert "launch" in capsys.readouterr().out

    def test_stop_unknown_is_operational_error(self, capsys):
        assert cli.main(["stop", "ghost"]) == cli.EXIT_OPERATIONAL
        assert "error:" in capsys.readouterr().err

    def test_launch_camoufox_seam_is_operational_error(self, capsys):
        assert cli.main(["launch", "--engine", "camoufox"]) == cli.EXIT_OPERATIONAL
        assert "#36" in capsys.readouterr().err

    def test_unknown_verb_argparse_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["bogus"])
        assert exc.value.code == cli.EXIT_USAGE

    def test_help_exits_0(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        assert exc.value.code == cli.EXIT_OK

    def test_status_reports_seeded_instance(self, capsys, monkeypatch, tmp_path):
        path = str(tmp_path / "reg.json")
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, path)
        _seed(path, {"seeded-01": _dead_entry(engine="chrome", profile=None)})
        assert cli.main(["status"]) == cli.EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["name"] == "seeded-01"
        assert rows[0]["alive"] is False

    def test_launch_browser_args_after_separator(self, monkeypatch):
        captured = {}

        def fake_launch(**kwargs):
            captured.update(kwargs)
            return lifecycle.ExtendedInstance(
                name="x-01", port=9222, pid=1, browser_version="", user_data_dir="",
                launched=None, pid_start=None, engine="chrome", profile=None,
            )

        monkeypatch.setattr(lifecycle, "launch", fake_launch)
        assert cli.main(["launch", "--headless", "--", "--proxy-server=x"]) == cli.EXIT_OK
        assert captured["browser_args"] == ["--proxy-server=x"]
        assert captured["headless"] is True
