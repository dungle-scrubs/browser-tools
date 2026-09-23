"""Exercise the locked real engine with Chrome captures, including absent navigations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from browser_tools import cli, insights, step_list
from browser_tools.usage import UsageError

FIXTURES = Path(__file__).parent / "fixtures" / "insights"


def invoke(capsys, *args):
    code = cli.main(["insights", *args])
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out else None, output.err


@pytest.fixture(scope="module")
def engine():
    # No network in ordinary tests. Missing setup is an explicit skip, never a
    # passing acceptance test. CI/acceptance runs must run --setup first.
    if not shutil.which("node") or not (insights.ADAPTER / ".installed").exists():
        pytest.skip("real engine unavailable: run bt insights --setup before this test")


def test_before_setup_names_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(insights, "ADAPTER", tmp_path)
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "nav.json"))
    assert code == 2 and document is None
    assert "bt insights --setup" in error and "Traceback" not in error


@pytest.mark.parametrize("name", ["load-only", "driven"])
def test_no_navigation_is_failure(engine, name, capsys):
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / f"{name}.json"))
    assert code == 1
    assert document["navigations"] == []
    assert document["browserVersion"] == "HeadlessChrome/151.0.7922.34"
    assert "Zero Insight Sets" in error and "Page.navigate" in error and "--steps" in error


@pytest.mark.parametrize(("name", "state"), [("nav", "pass"), ("nav-click", "fail")])
def test_navigation_interaction_difference(engine, capsys, name, state):
    code, document, error = invoke(
        capsys, "--trace", str(FIXTURES / f"{name}.json"), "--insight", "INPBreakdown"
    )
    assert code == 0, error
    models = document["navigations"][0]["insights"]
    assert len(models) == 1 and models[0]["state"] == state
    assert models[0]["key"] == "INPBreakdown"
    assert isinstance(models[0]["title"], str)
    assert models[0]["savings"] is None
    detail = models[0]["detail"]
    if state == "fail":
        for text in ["Input delay:", "Processing duration:", "Presentation delay:", "280"]:
            assert text in detail
    else:
        assert "The longest interaction on the page" not in detail


def test_http_probe_has_all_models(engine, capsys):
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "http-navigation.json"))
    assert code == 0, error
    assert document["engineRevision"] == "0.0.65"
    assert document["browserVersion"] == "HeadlessChrome/153.0.0.0"
    models = {model["key"]: model for model in document["navigations"][0]["insights"]}
    assert len(models) == 19
    assert models["RenderBlocking"]["state"] == "fail"
    assert "[object Object]" not in json.dumps(document)
    assert set(models["RenderBlocking"]) == {"key", "state", "title", "savings", "detail"}


def test_model_errors_are_not_silently_dropped(engine, capsys):
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "nav.json"))
    assert code == 1
    assert "DocumentLatency: missing document request timing" in error
    assert len(document["navigations"][0]["insights"]) == 18


def test_unknown_name_lists_valid_names(engine, capsys):
    code, _, error = invoke(
        capsys, "--trace", str(FIXTURES / "nav.json"), "--insight", "NotAnInsight"
    )
    assert code == 2
    assert "Valid names:" in error and "INPBreakdown" in error and "RenderBlocking" in error


@pytest.mark.parametrize("version", ["Chrome/999.0.0.0", None])
def test_version_refusal_and_override(engine, capsys, tmp_path, version):
    trace = json.loads((FIXTURES / "nav-click.json").read_text())
    trace["metadata"] = {} if version is None else {"user-agent": version}
    path = tmp_path / "trace with spaces.json"
    path.write_text(json.dumps(trace))
    args = ["--trace", str(path), "--insight", "INPBreakdown"]
    code, refused, error = invoke(capsys, *args)
    assert code == 1 and "--ignore-engine-mismatch" in error
    assert refused["browserVersion"] == version
    assert refused["engineRevision"] == "0.0.65"
    code, accepted, error = invoke(capsys, *args, "--ignore-engine-mismatch")
    assert code == 0, error
    assert accepted["browserVersion"] == version
    assert accepted["engineRevision"] == refused["engineRevision"]
    assert accepted["navigations"][0]["insights"][0]["state"] == "fail"


def test_text_is_only_upstream_detail(engine, capsys):
    args = ["--trace", str(FIXTURES / "nav-click.json"), "--insight", "INPBreakdown"]
    _, document, _ = invoke(capsys, *args)
    assert cli.main(["insights", *args, "--format", "text"]) == 0
    output = capsys.readouterr()
    assert output.out.strip() == document["navigations"][0]["insights"][0]["detail"]
    assert output.err == ""


@pytest.mark.parametrize("payload", ["{broken", "{}", "null", '{"traceEvents":[null]}'])
def test_bad_input_is_diagnostic(engine, tmp_path, capsys, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload)
    code, _, error = invoke(capsys, "--trace", str(path))
    assert code == 1 and "trace" in error.lower() and "Traceback" not in error


def test_missing_input_is_diagnostic(engine, tmp_path, capsys):
    code, _, error = invoke(capsys, "--trace", str(tmp_path / "absent.json"))
    assert code == 1 and "Cannot analyze trace" in error


def test_insights_is_not_a_step():
    with pytest.raises(UsageError, match="local trace"):
        step_list.validate("insights --trace trace.json")


def test_analysis_never_calls_npm(engine, tmp_path):
    # Only Node is on PATH. Even npm cannot be found, yet an installed engine works.
    node = shutil.which("node")
    assert node
    (tmp_path / "node").symlink_to(node)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "browser_tools.cli",
            "insights",
            "--trace",
            str(FIXTURES / "http-navigation.json"),
        ],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["navigations"][0]["insights"]) == 19


def test_setup_failure_does_not_leave_marker(tmp_path, monkeypatch, capsys):
    for name in ["package.json", "package-lock.json"]:
        shutil.copy(insights.ADAPTER / name, tmp_path / name)
    monkeypatch.setattr(insights, "ADAPTER", tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: f"/tools/{name}")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "v22.20.0", "")
        return subprocess.CompletedProcess(command, 1, "", "network unavailable")

    monkeypatch.setattr(subprocess, "run", run)
    code, _, error = invoke(capsys, "--setup")
    assert code == 1 and "npm ci --omit=dev" in error and "network" in error
    assert not (tmp_path / ".installed").exists()
    assert calls[1][0][1:3] == ["ci", "--omit=dev"]
    assert calls[1][1]["cwd"] == tmp_path


def test_changed_lock_requires_setup(engine, tmp_path, monkeypatch, capsys):
    directory = tmp_path / "adapter"
    shutil.copytree(insights.ADAPTER, directory, ignore=shutil.ignore_patterns("node_modules"))
    (directory / "node_modules").symlink_to(
        insights.ADAPTER / "node_modules", target_is_directory=True
    )
    manifest = directory / "package-lock.json"
    manifest.write_text(manifest.read_text() + "\n")
    monkeypatch.setattr(insights, "ADAPTER", directory)
    code, _, error = invoke(capsys, "--trace", str(FIXTURES / "nav.json"))
    assert code == 2 and "bt insights --setup" in error


def test_setup_options_are_exclusive(capsys):
    assert cli.main(["insights", "--setup", "--insight", "INPBreakdown"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_bare_insights_names_setup(capsys):
    code, document, error = invoke(capsys)
    assert code == 2 and document is None
    assert "bt insights --setup" in error
