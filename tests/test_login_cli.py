"""The ``bt login`` grammar: dispatch, exit codes, refusals, and no leaks.

Everything here runs through ``cli.main`` (or ``step_list.validate`` for the
Step Run refusal) with fakes standing in for the browser session and the
relay, so a test proves what a shell would see: the exit code, the JSON on
stdout, the fixed diagnostic on stderr, and the absence of any sentinel
credential in either channel.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
from typing import Any

import pytest

from browser_tools import cli, credential_relay, login_ownership
from browser_tools import login as login_module
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError
from browser_tools.login_policy import LoginPolicyError
from browser_tools.step_list import StepListError
from browser_tools.step_list import validate as validate_steps

SENTINEL_USER = "SENTINEL-user"
SENTINEL_PASS = "SENTINEL-pass"
VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"


def policy_file(tmp_path, *, origin: str = "https://example.com") -> str:
    document = {
        "version": 1,
        "identity": "browser",
        "vault": VAULT,
        "accounts": {
            "example": {
                "item": ITEM,
                "origin": origin,
                "username_selector": "#username",
                "password_selector": "#password",
                "submit_selector": "button[type=submit]",
                "success_selector": "[data-signed-in]",
            }
        },
    }
    path = tmp_path / "logins.json"
    path.write_text(json.dumps(document))
    return str(path)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = str(tmp_path / "registry.json")
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", path)

    def seed(**entries: dict) -> str:
        core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
            dict(entries), path
        )
        return path

    seed()
    return seed


def chrome_entry() -> dict:
    return {
        "port": 9500,
        "pid": 2_000_000_000,
        "browser_version": "Chrome/1",
        "user_data_dir": "",
        "engine": "chrome",
    }


@pytest.fixture
def owned_instance(monkeypatch):
    """Stand past the ownership boundary in CLI-level runs.

    The registry rows here carry synthetic PIDs; the ownership module's own
    tests and the live module cover the real check.
    """
    monkeypatch.setattr(
        login_module.login_ownership, "verify_recorded_owner",
        lambda entry, deadline: login_ownership.OwnershipSnapshot(
            pid=2_000_000_000, pid_start="START",
            user_data_dir="/recorded/session-dir", port=9500,
        ),
    )


@pytest.fixture
def login_stub(monkeypatch):
    """Record what login.login receives; return the success document."""
    seen: dict[str, Any] = {}

    def stub(**kwargs):
        seen.clear()
        seen.update(kwargs)
        return {"account": kwargs["account"], "status": "authenticated"}

    monkeypatch.setattr(login_module, "login", stub)
    return seen


class TestGrammar:
    def test_the_verb_parses_and_dispatches(self, registry, login_stub, tmp_path):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(
            ["login", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 0
        assert login_stub["instance"] is None
        assert login_stub["account"] == "example"
        assert login_stub["timeout_ms"] == login_module.DEFAULT_LOGIN_TIMEOUT_MS
        assert login_stub["target"] is None

    def test_account_is_required(self, registry, capsys):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(["login"]) == 2
        assert "--account" in capsys.readouterr().err

    def test_timeout_must_be_finite_positive(self, registry, capsys, tmp_path):
        registry(**{"web-01": chrome_entry()})
        # Preconditions bt owns return exit 2; argparse-owned shape errors
        # exit 2 through SystemExit, which is what a shell also sees.
        for bad in ("0", "-5"):
            assert cli.main(
                ["login", "--account", "x", "--timeout-ms", bad, "--config", policy_file(tmp_path)]
            ) == 2, bad
            assert "timeout-ms" in capsys.readouterr().err
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["login", "--account", "x", "--timeout-ms", "notanumber"])
        assert excinfo.value.code == 2

    def test_a_huge_timeout_is_a_usage_error_not_a_traceback(self, registry, capsys, tmp_path):
        """argparse accepts an int no float can hold; that is exit 2.

        The old check called math.isfinite on it, which converts to float
        first and raised OverflowError - a traceback for a usage problem.
        """
        registry(**{"web-01": chrome_entry()})
        huge = "1" + "0" * 400
        assert cli.main(
            ["login", "--account", "x", "--timeout-ms", huge, "--config", policy_file(tmp_path)]
        ) == 2
        assert "timeout-ms" in capsys.readouterr().err
        with pytest.raises(LoginPolicyError, match="timeout-ms"):
            login_module.login(
                instance="web-01", account="x",
                config=policy_file(tmp_path), timeout_ms=10 ** 400,
                registry_path=os.environ["BROWSER_TOOLS_REGISTRY"],
            )

    def test_leading_instance_syntax_dispatches(self, registry, login_stub, tmp_path):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(
            ["web-01", "login", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 0
        assert login_stub["instance"] == "web-01"

    def test_target_chooses_index_or_id_prefix(self, registry, login_stub, tmp_path):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(
            ["login", "web-01", "--account", "example", "--target", "2",
             "--config", policy_file(tmp_path)]
        ) == 0
        assert login_stub["target"] == "2"

    def test_no_secret_or_endpoint_flags_exist(self, registry, capsys):
        registry(**{"web-01": chrome_entry()})
        for flag in ("--endpoint", "--url", "--text", "--pass-token"):
            with pytest.raises(SystemExit) as excinfo:
                cli.main(["login", "--account", "x", flag, "v"])
            assert excinfo.value.code == 2, flag
            assert "unrecognized arguments" in capsys.readouterr().err

    def test_login_is_a_known_verb_for_instance_disambiguation(self):
        assert "login" in cli.KNOWN_VERBS


class TestExitCodes:
    def test_unknown_alias_is_usage_exit_2(self, registry, tmp_path, capsys, monkeypatch):
        registry(**{"web-01": chrome_entry()})
        # No stub: the real policy load refuses before any browser access.
        opened = []

        @contextlib.asynccontextmanager
        async def trapped(port, spec, by):
            opened.append(True)
            raise LifecycleError("session")

        monkeypatch.setattr(login_module, "one_shot_page_session", trapped)
        assert cli.main(
            ["login", "--account", "ghost", "--config", policy_file(tmp_path)]
        ) == 2
        err = capsys.readouterr().err
        assert "no login account" in err
        # The diagnostic names no alias: not the requested one, and not the
        # configured ones (policy text stays out of the output channels).
        assert "ghost" not in err
        assert "example" not in err
        assert opened == []

    def test_malformed_config_is_usage_exit_2(self, registry, tmp_path, capsys):
        registry(**{"web-01": chrome_entry()})
        bad = tmp_path / "bad.json"
        bad.write_text("{")
        assert cli.main(["login", "--account", "x", "--config", str(bad)]) == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_missing_config_is_usage_exit_2(self, registry, tmp_path, capsys):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(
            ["login", "--account", "x", "--config", str(tmp_path / "none.json")]
        ) == 2
        assert "not found" in capsys.readouterr().err

    def test_operational_failure_is_exit_1(self, registry, login_stub, monkeypatch, tmp_path, capsys):
        registry(**{"web-01": chrome_entry()})

        def failing(**kwargs):
            raise LifecycleError("the form was submitted but the configured success condition "
                                 "was not met within the timeout")

        monkeypatch.setattr(login_module, "login", failing)
        assert cli.main(
            ["login", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "success condition" in err

    def test_success_prints_exactly_the_small_document(self, registry, login_stub, tmp_path, capsys):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(
            ["login", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}


class TestPreBrowserRefusals:
    """Done-when 1: every fail-closed shape refuses before any credential."""

    @pytest.fixture
    def relay_trap(self, monkeypatch):
        calls = []

        def trap(**kwargs):
            calls.append(kwargs)
            raise AssertionError("a credential was read on a refuse-first path")

        monkeypatch.setattr(credential_relay, "read_reference", trap)
        monkeypatch.setattr(credential_relay, "find_opchain", lambda: "/fake/opchain")
        monkeypatch.setattr(credential_relay, "ensure_relay_supported", lambda *a, **k: None)
        return calls

    def _open_session_trap(self, monkeypatch):
        opened = []

        @contextlib.asynccontextmanager
        async def trapped(port, spec, by):
            opened.append(True)
            raise LifecycleError("session")

        monkeypatch.setattr(login_module, "one_shot_page_session", trapped)
        return opened

    def test_unknown_alias_reads_no_credential(self, registry, relay_trap, tmp_path):
        registry(**{"web-01": chrome_entry()})
        with pytest.raises(LoginPolicyError):
            login_module.login(
                instance="web-01", account="ghost",
                config=policy_file(tmp_path), registry_path=os.environ["BROWSER_TOOLS_REGISTRY"],
            )
        assert relay_trap == []

    def test_camoufox_engine_is_refused_without_credentials(
        self, registry, relay_trap, monkeypatch, tmp_path
    ):
        entry = chrome_entry()
        entry["engine"] = "camoufox"
        registry(**{"fox-01": entry})
        opened = self._open_session_trap(monkeypatch)
        monkeypatch.setattr(
            credential_relay, "ensure_relay_supported",
            lambda *args, **kwargs: pytest.fail("engine refusal must precede the relay probe"),
        )
        with pytest.raises(LifecycleError, match="only with Chrome"):
            login_module.login(
                instance="fox-01", account="example",
                config=policy_file(tmp_path), registry_path=os.environ["BROWSER_TOOLS_REGISTRY"],
            )
        assert opened == []
        assert relay_trap == []

    def test_missing_opchain_is_refused_without_a_browser(
        self, registry, monkeypatch, tmp_path, owned_instance
    ):
        registry(**{"web-01": chrome_entry()})
        monkeypatch.setattr(credential_relay, "find_opchain", lambda: None)
        opened = self._open_session_trap(monkeypatch)
        with pytest.raises(LifecycleError, match="not installed"):
            login_module.login(
                instance="web-01", account="example",
                config=policy_file(tmp_path), registry_path=os.environ["BROWSER_TOOLS_REGISTRY"],
            )
        assert opened == []


class TestStepRunRefusal:
    def test_login_cannot_be_a_step(self):
        with pytest.raises(StepListError, match="cannot be a step"):
            validate_steps("login --account example\n")

    def test_a_run_refuses_login_before_any_step(self, registry, tmp_path, capsys, login_stub):
        registry(**{"web-01": chrome_entry()})
        steps = tmp_path / "steps.txt"
        steps.write_text("login --account example\nsnapshot\n")
        assert cli.main(["run", str(steps)]) == 2
        assert login_stub == {}
        assert "cannot be a step" in capsys.readouterr().err

    def test_trace_steps_refuses_login_too(self, registry, tmp_path, capsys, login_stub):
        registry(**{"web-01": chrome_entry()})
        steps = tmp_path / "steps.txt"
        steps.write_text("login --account example\n")
        out = tmp_path / "trace.json"
        assert cli.main(["trace", "--steps", str(steps), "--out", str(out)]) == 2
        assert login_stub == {}
        assert not out.exists()


class TestNoLeaksThroughTheCLI:
    def _full_stub(self, monkeypatch):
        """A login implementation that fails with sentinel-bearing internals."""

        def failing(**kwargs):
            try:
                raise RuntimeError(f"inner detail with {SENTINEL_USER}")
            except RuntimeError as exc:
                raise LifecycleError("the browser session failed during login (form fill)") from exc

        monkeypatch.setattr(login_module, "login", failing)

    def test_failure_paths_print_no_sentinel(self, registry, monkeypatch, tmp_path, capsys):
        registry(**{"web-01": chrome_entry()})
        self._full_stub(monkeypatch)
        assert cli.main(
            ["login", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 1
        captured = capsys.readouterr()
        assert "SENTINEL" not in captured.out + captured.err

    def test_success_path_prints_no_sentinel_even_with_odd_aliases(
        self, registry, login_stub, tmp_path, capsys
    ):
        registry(**{"web-01": chrome_entry()})
        assert cli.main(
            ["login", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 0
        captured = capsys.readouterr()
        assert "SENTINEL" not in captured.out


class TestUnstartableProvider:
    """An opchain that cannot even be spawned is one operational fact.

    The round-two reproduction: an executable-format-invalid fake opchain
    raised OSError(ENOEXEC) out of the CLI as a traceback. Both Popen call
    sites normalize any spawn OSError into the fixed RelayError family,
    cause suppressed, so the shell sees exit 1 and a fixed sentence.
    """

    def test_executable_format_invalid_opchain_is_exit_1_not_a_traceback(
        self, registry, monkeypatch, tmp_path, capsys, owned_instance
    ):
        registry(**{"web-01": chrome_entry()})
        binary = tmp_path / "opchain"
        binary.write_text("This deliberately is not an executable format.\n")
        binary.chmod(0o700)
        monkeypatch.setattr(credential_relay, "find_opchain", lambda: str(binary))
        assert cli.main(
            ["login", "web-01", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 1
        err = capsys.readouterr().err
        assert "could not be started" in err
        assert "Traceback" not in err
        assert str(tmp_path) not in err
        assert "opchain" in err

    def test_injected_oserror_with_sentinel_text_is_exit_1_clean(
        self, registry, monkeypatch, tmp_path, capsys, owned_instance
    ):
        registry(**{"web-01": chrome_entry()})
        monkeypatch.setattr(credential_relay, "find_opchain", lambda: "/fake/opchain")

        def explode(*args, **kwargs):
            raise OSError(errno.ENOEXEC, "SENTINEL-provider-burst /fake/opchain")

        monkeypatch.setattr(credential_relay.subprocess, "Popen", explode)
        assert cli.main(
            ["login", "web-01", "--account", "example", "--config", policy_file(tmp_path)]
        ) == 1
        captured = capsys.readouterr()
        assert "could not be started" in captured.err
        assert "SENTINEL" not in captured.out + captured.err
        assert "Traceback" not in captured.err
        assert "/fake/opchain" not in captured.err
