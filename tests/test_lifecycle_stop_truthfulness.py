import json
import subprocess
from pathlib import Path

import pytest

from browser_tools import lifecycle, process_utils


def test_helper_exit_before_identity_read_allows_verified_retirement(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run
    closed = []

    def run(argv, **kwargs):
        if "-e" in argv:
            output = (
                "123 unrelated\n"
                if closed
                else (
                    f"2000000000 chrome --user-data-dir={session}\n"
                    f"2000000001 chrome-helper --user-data-dir={session}\n"
                )
            )
            return subprocess.CompletedProcess(argv, 0, output, "")
        if closed and "lstart=" in argv and "2000000001" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if closed and "stat=" in argv:
            return subprocess.CompletedProcess(argv, 0, "Z\n", "")
        if closed and argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original_run(argv, **kwargs)

    def kill(pid, sig):
        signals.append((pid, sig))
        if closed and pid == 2000000001:
            raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", closed.append)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: False)

    assert lifecycle.stop("owned-01", registry_path=registry) == "Stopped owned-01"
    assert closed == [19423]
    assert all(sig == 0 for _, sig in signals)
    assert lifecycle.read_instances(registry) == []
    assert not session.exists()


def seed(tmp_path, *, profile=None):
    session = tmp_path / "session"
    session.mkdir()
    (session / "recoverable").write_text("keep")
    registry = tmp_path / "registry.json"
    entry = {
        "port": 19423,
        "pid": 2000000000,
        "pid_start": "original",
        "browser_version": "Chrome/1",
        "user_data_dir": str(session),
        "launched": "2026-01-01",
        "engine": "chrome",
        "profile": profile,
    }
    registry.write_text(json.dumps({"owned-01": entry}))
    return str(registry), session


def surviving_os(monkeypatch, session):
    def run(argv, **kwargs):
        if argv[0] == "ps":
            if "lstart=" in argv:
                output = "original\n"
            elif "stat=" in argv:
                output = "S\n"
            elif "-e" in argv:
                output = (
                    f"2000000000 chrome --remote-debugging-port=19423 --user-data-dir={session}\n"
                )
            else:
                output = f"chrome --remote-debugging-port=19423 --user-data-dir={session}\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, "2000000000\n", "")
        raise AssertionError(argv)

    signals = []
    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", lambda port: None)
    clock = iter(range(100000))
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: None)
    return signals


def test_owned_survivor_preserves_profile_and_registry(tmp_path, monkeypatch):
    registry, session = seed(tmp_path, profile="named")
    surviving_os(monkeypatch, session)

    with pytest.raises(lifecycle.LifecycleError, match=r"exit|surviv|unknown"):
        lifecycle.stop("owned-01", registry_path=registry)

    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_cli_survivor_is_operational_failure_with_ephemeral_state_retained(
    tmp_path, monkeypatch, capsys
):
    from browser_tools import cli
    from browser_tools.core import cdp_client

    registry, session = seed(tmp_path)
    surviving_os(monkeypatch, session)
    monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, registry)

    async def unavailable(**kwargs):
        raise ConnectionError("disposable browser is frozen")

    monkeypatch.setattr(cdp_client, "get_ws_url_async", unavailable)
    assert cli.main(["stop", "owned-01"]) == cli.EXIT_OPERATIONAL
    output = capsys.readouterr()
    assert '"stopped": true' not in output.out
    assert "owned-01" in output.err
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_replacement_browser_survival_is_not_recorded_pid_exit(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run

    def run(argv, **kwargs):
        result = original_run(argv, **kwargs)
        return subprocess.CompletedProcess(
            argv, result.returncode, result.stdout.replace("2000000000", "2000000001"), ""
        )

    def kill(pid, sig):
        if pid == 2000000000:
            raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: False)
    with pytest.raises(lifecycle.LifecycleError, match=r"exit|surviv|unknown"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_unattributed_listener_prevents_retirement_without_closing_it(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "lsof" or "-e" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return original_run(argv, **kwargs)

    def gone(pid, sig):
        if sig:
            signals.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", gone)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: True)
    closes = []
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", closes.append)
    with pytest.raises(lifecycle.LifecycleError, match="unknown"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert closes == []
    assert signals == []
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_supervisor_survival_preserves_browser_recovery_state(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    surviving_os(monkeypatch, session)
    entry = json.loads(Path(registry).read_text())
    entry["owned-01"].update(supervisor_pid=2000000000, supervisor_pid_start="original")
    Path(registry).write_text(json.dumps(entry))
    with pytest.raises(lifecycle.LifecycleError, match="supervisor"):
        lifecycle.stop(
            "owned-01", registry_path=registry, _supervisor_term_wait=0.1, _supervisor_kill_wait=0.1
        )
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


@pytest.mark.parametrize("profile", [None, "named"])
@pytest.mark.parametrize("parent_name", ["normal", "parent with spaces"])
def test_graceful_exit_retires_only_ephemeral_data_even_with_unreaped_pid(
    tmp_path, monkeypatch, profile, parent_name
):
    parent = tmp_path / parent_name
    parent.mkdir()
    registry, session = seed(parent, profile=profile)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run
    closed = []

    def run(argv, **kwargs):
        if closed and "stat=" in argv:
            return subprocess.CompletedProcess(argv, 0, "Z\n", "")
        if closed and argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", closed.append)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: False)
    outcome = lifecycle.stop("owned-01", registry_path=registry)
    assert outcome.startswith("Stopped owned-01")
    assert closed == [19423]
    assert all(sig == 0 for _, sig in signals)
    assert lifecycle.read_instances(registry) == []
    if profile is None:
        assert not session.exists()
    else:
        assert (session / "recoverable").read_text() == "keep"


def test_stranger_listener_is_not_closed_or_signalled_while_owned_browser_exits(
    tmp_path, monkeypatch
):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run
    exited = []
    closes = []

    def run(argv, **kwargs):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, "2000000001\n", "")
        if "2000000001" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                "chrome --remote-debugging-port=19423 --user-data-dir=/disposable/stranger\n",
                "",
            )
        if exited and "-e" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "2000000001 chrome --user-data-dir=/disposable/stranger\n", ""
            )
        return original_run(argv, **kwargs)

    def kill(pid, sig):
        signals.append((pid, sig))
        if pid == 2000000000 and exited:
            raise ProcessLookupError
        if pid == 2000000000 and sig:
            exited.append(pid)

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", closes.append)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: True)
    assert lifecycle.stop("owned-01", registry_path=registry) == "Stopped owned-01"
    assert closes == []
    assert [(pid, sig) for pid, sig in signals if sig] == [(2000000000, 15)]
    assert lifecycle.read_instances(registry) == []
    assert not session.exists()


def test_listener_inspection_failure_keeps_recoverable_state(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "lsof":
            raise subprocess.TimeoutExpired(argv, 2)
        if "-e" in argv:
            return subprocess.CompletedProcess(argv, 0, "2000000001 unrelated\n", "")
        return original_run(argv, **kwargs)

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", gone)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: False)
    with pytest.raises(lifecycle.LifecycleError, match="unknown"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_unknown_supervisor_ownership_preserves_live_browser_without_signalling(
    tmp_path, monkeypatch
):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    entry = json.loads(Path(registry).read_text())
    entry["owned-01"].update(supervisor_pid=2000000002, supervisor_pid_start="original")
    Path(registry).write_text(json.dumps(entry))

    def kill(pid, sig):
        if pid == 2000000002:
            raise PermissionError
        signals.append((pid, sig))

    monkeypatch.setattr(lifecycle.os, "kill", kill)
    with pytest.raises(lifecycle.LifecycleError, match="supervisor"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert all(sig == 0 for _, sig in signals)
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_identity_change_after_term_never_signals_replacement_at_escalation(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run
    termed = []

    def run(argv, **kwargs):
        if termed and "lstart=" in argv:
            return subprocess.CompletedProcess(argv, 0, "replacement\n", "")
        return original_run(argv, **kwargs)

    def kill(pid, sig):
        signals.append((pid, sig))
        if sig == 15:
            termed.append(pid)

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    with pytest.raises(lifecycle.LifecycleError, match="unknown"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert [(pid, sig) for pid, sig in signals if sig] == [(2000000000, 15)]
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_empty_process_snapshot_is_unknown_not_verified_exit(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    surviving_os(monkeypatch, session)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1 if argv[0] == "lsof" else 0, "", "")

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", gone)
    with pytest.raises(lifecycle.LifecycleError, match="unknown"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_missing_directory_identity_preserves_recovery_record(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    surviving_os(monkeypatch, session)
    raw = json.loads(Path(registry).read_text())
    raw["owned-01"]["user_data_dir"] = ""
    Path(registry).write_text(json.dumps(raw))

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1 if argv[0] == "lsof" else 0, "123 unrelated\n" if "-e" in argv else "", ""
        )

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", gone)
    with pytest.raises(lifecycle.LifecycleError, match="unknown"):
        lifecycle.stop("owned-01", registry_path=registry)
    assert [inst.name for inst in lifecycle.read_instances(registry)] == ["owned-01"]
    assert (session / "recoverable").read_text() == "keep"


def test_cdp_exit_between_liveness_and_identity_read_retires_without_signal(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run
    closed = []
    gone = []

    def run(argv, **kwargs):
        if closed and "lstart=" in argv:
            gone.append(True)
            return subprocess.CompletedProcess(argv, 1, "", "")
        if closed and "stat=" in argv:
            return subprocess.CompletedProcess(argv, 0, "R\n", "")
        if closed and "-e" in argv:
            return subprocess.CompletedProcess(argv, 0, "123 unrelated\n", "")
        if closed and argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original_run(argv, **kwargs)

    def kill(pid, sig):
        signals.append((pid, sig))
        if gone:
            raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", closed.append)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: False)

    assert lifecycle.stop("owned-01", registry_path=registry) == "Stopped owned-01"
    assert closed == [19423]
    assert all(sig == 0 for _, sig in signals)
    assert lifecycle.read_instances(registry) == []
    assert not session.exists()


def test_cdp_exit_finishes_after_identity_disappears_without_unsafe_signal(tmp_path, monkeypatch):
    registry, session = seed(tmp_path)
    signals = surviving_os(monkeypatch, session)
    original_run = process_utils.subprocess.run
    closed = []
    gone = []
    observations = []

    def run(argv, **kwargs):
        if closed and "lstart=" in argv:
            gone.append(True)
            return subprocess.CompletedProcess(argv, 1, "", "")
        if closed and "stat=" in argv:
            return subprocess.CompletedProcess(argv, 0, "R\n", "")
        if closed and "-e" in argv:
            return subprocess.CompletedProcess(argv, 0, "123 unrelated\n", "")
        if closed and argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original_run(argv, **kwargs)

    def kill(pid, sig):
        signals.append((pid, sig))
        if gone:
            observations.append(pid)
            if len(observations) > 3:
                raise ProcessLookupError

    monkeypatch.setattr(process_utils.subprocess, "run", run)
    monkeypatch.setattr(lifecycle.os, "kill", kill)
    monkeypatch.setattr(lifecycle, "_try_cdp_browser_close", closed.append)
    monkeypatch.setattr(lifecycle.core_registry, "_port_is_listening", lambda port: False)

    assert lifecycle.stop("owned-01", registry_path=registry) == "Stopped owned-01"
    assert closed == [19423]
    assert all(sig == 0 for _, sig in signals)
    assert lifecycle.read_instances(registry) == []
    assert not session.exists()
