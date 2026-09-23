"""Exercise the locked real engine with Chrome captures, including absent navigations."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from browser_tools import cli, insights, step_list
from browser_tools.usage import UsageError

FIXTURES = Path(__file__).parent / "fixtures" / "insights"


def invoke(capsys, *args):
    code = cli.main(["insights", *args])
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out else None, output.err


def adapter_copy(tmp_path, *, node_modules=False, installed=False):
    """A copy of the shipped adapter, carrying the package files a wheel ships.

    An empty directory is not a plausible adapter state: every file named in
    ADAPTER_FILES comes from the wheel and no npm command can create one.
    """
    directory = tmp_path / "adapter"
    shutil.copytree(
        insights.ADAPTER,
        directory,
        ignore=shutil.ignore_patterns("node_modules", ".installed", ".setup.lock"),
    )
    if node_modules:
        (directory / "node_modules").symlink_to(
            insights.ADAPTER / "node_modules", target_is_directory=True
        )
    if installed:
        (directory / ".installed").write_text(insights._lock_digest(directory))
    return directory


def refuse_subprocess(*args, **kwargs):
    raise AssertionError(f"no process may start for this input: {args!r}")


@pytest.fixture(scope="module")
def engine():
    # No network in ordinary tests. Missing setup is an explicit skip, never a
    # passing acceptance test. CI/acceptance runs must run --setup first.
    if not shutil.which("node") or not (insights.ADAPTER / ".installed").exists():
        pytest.skip("real engine unavailable: run bt insights --setup before this test")


def test_before_setup_names_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(insights, "ADAPTER", adapter_copy(tmp_path))
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "nav.json"))
    assert code == 2 and document is None
    assert "bt insights --setup" in error and "Traceback" not in error
    # The RFC error table promises the network requirement in this message.
    assert "network" in error and "npm registry" in error


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
    # One insight per navigation. These fixtures have exactly one navigation;
    # two-navigations.json covers the case where that is not so.
    assert len(document["navigations"]) == 1
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
    directory = adapter_copy(tmp_path)
    monkeypatch.setattr(insights, "ADAPTER", directory)
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
    assert not (directory / ".installed").exists()
    assert calls[1][0][1:3] == ["ci", "--omit=dev"]
    # The engine declares its own dependencies as "latest", so the lockfile is
    # the supply-chain control. A lifecycle script would run around it.
    assert "--ignore-scripts" in calls[1][0]
    assert calls[1][1]["cwd"] == directory


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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # A navigation is in the trace as a Document request, but the category
        # list dropped blink.user_timing, which carries navigationStart. The
        # old fixed string told this caller to capture the navigate step they
        # had already captured.
        ("narrow-categories", ["blink.user_timing", "--categories", "document request"]),
        # navigationStart is there and committed nothing: the 404 navigation
        # never produced a document inside the trace window.
        ("uncommitted-navigation", ["navigationStart", "documentLoaderURL is empty"]),
        # Genuinely no navigation. This is the only case the old string fitted.
        ("load-only", ["no navigation at all", "Page.navigate", "--steps"]),
        ("driven", ["no navigation at all", "Page.navigate", "--steps"]),
    ],
)
def test_zero_sets_names_the_cause_that_is_true(engine, capsys, name, expected):
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / f"{name}.json"))
    assert code == 1
    assert document["navigations"] == []
    assert "Zero Insight Sets" in error
    for text in expected:
        assert text in error, error
    if name in ("narrow-categories", "uncommitted-navigation"):
        # Never print a remedy that reproduces the failure.
        assert "Capture a Page.navigate step" not in error


def test_error_page_navigation_is_not_reported_as_success(engine, capsys):
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "error-page.json"))
    assert code == 1
    assert "chrome-error://chromewebdata/" in error and "never loaded" in error
    # The analysis is retained, as it is for model errors.
    navigation = document["navigations"][0]
    assert navigation["url"] == "chrome-error://chromewebdata/"
    assert len(navigation["insights"]) == 19
    # A failed run prints the stamped document, not pages of text over an
    # error page.
    assert cli.main(["insights", "--trace", str(FIXTURES / "error-page.json"), "--format", "text"]) == 1
    assert json.loads(capsys.readouterr().out)["navigations"][0]["url"].startswith("chrome-error:")


def test_selected_insight_is_one_per_navigation(engine, capsys):
    code, document, error = invoke(
        capsys, "--trace", str(FIXTURES / "two-navigations.json"), "--insight", "INPBreakdown"
    )
    assert code == 0, error
    assert len(document["navigations"]) == 2
    assert [len(navigation["insights"]) for navigation in document["navigations"]] == [1, 1]


def test_text_names_each_navigation_when_there_is_more_than_one(engine, capsys):
    trace = str(FIXTURES / "two-navigations.json")
    _, document, _ = invoke(capsys, "--trace", trace, "--insight", "INPBreakdown")
    assert cli.main(["insights", "--trace", trace, "--insight", "INPBreakdown", "--format", "text"]) == 0
    output = capsys.readouterr().out
    for index, navigation in enumerate(document["navigations"], start=1):
        assert f"# Navigation {index} of 2: {navigation['url']}" in output
        assert navigation["insights"][0]["detail"] in output


def test_version_gate_needs_a_whole_product_token(engine, tmp_path, capsys):
    trace = json.loads((FIXTURES / "nav-click.json").read_text())
    trace["metadata"] = {"product-version": "evil.com/Chrome/153.0.0.0"}
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(trace))
    code, document, error = invoke(capsys, "--trace", str(path))
    assert code == 1 and "--ignore-engine-mismatch" in error
    assert document["browserVersion"] is None


def test_the_printed_recovery_command_is_valid_shell(tmp_path):
    script = tmp_path / "remedy.sh"
    script.write_text(insights.setup_remedy() + "\n")
    parsed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr


def test_damaged_adapter_names_the_repair_that_works(tmp_path, monkeypatch, capsys):
    directory = adapter_copy(tmp_path, node_modules=True, installed=True)
    shutil.rmtree(directory / "vendor")
    monkeypatch.setattr(insights, "ADAPTER", directory)
    # Setup used to answer ready over this tree and restore nothing, so the
    # analysis failure sent the caller back to the command that just passed.
    code, _, error = invoke(capsys, "--setup")
    assert code == 1
    assert "vendor/PerformanceInsightFormatter.js" in error
    assert insights.REINSTALL_COMMAND in error and insights.SETUP_COMMAND in error
    code, _, error = invoke(capsys, "--trace", str(FIXTURES / "nav.json"))
    assert code == 1 and insights.REINSTALL_COMMAND in error


def test_node_stderr_reaches_the_diagnostic(engine, tmp_path, monkeypatch, capsys):
    directory = adapter_copy(tmp_path, node_modules=True, installed=True)
    (directory / "adapter.mjs").write_text(
        "process.stderr.write('ERR_MODULE_NOT_FOUND: the runtime is gone\\n');\n"
        "process.exit(1);\n"
    )
    monkeypatch.setattr(insights, "ADAPTER", directory)
    code, _, error = invoke(capsys, "--trace", str(FIXTURES / "nav.json"))
    assert code == 1
    assert "ERR_MODULE_NOT_FOUND: the runtime is gone" in error


def test_engine_stderr_alone_is_still_not_failure(engine, tmp_path, monkeypatch, capsys):
    directory = adapter_copy(tmp_path, node_modules=True, installed=True)
    source = (insights.ADAPTER / "adapter.mjs").read_text()
    (directory / "adapter.mjs").write_text(
        "process.stderr.write('LanternError: Invalid rtt NaN\\n');\n" + source
    )
    monkeypatch.setattr(insights, "ADAPTER", directory)
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "http-navigation.json"))
    assert code == 0, error
    assert len(document["navigations"][0]["insights"]) == 19


@pytest.mark.parametrize("kind", ["fifo", "directory", "empty"])
def test_an_unreadable_trace_path_is_refused_before_node(
    engine, tmp_path, monkeypatch, capsys, kind
):
    monkeypatch.setattr(subprocess, "run", refuse_subprocess)
    if kind == "fifo":
        path = tmp_path / "trace.fifo"
        os.mkfifo(path)
        expected = "FIFO"
    elif kind == "directory":
        path = tmp_path
        expected = "directory"
    else:
        path = ""
        expected = "empty"
    code, document, error = invoke(capsys, "--trace", str(path))
    assert code == 2 and document is None
    assert expected in error and "Traceback" not in error


def test_dev_dependencies_are_not_required_after_omit_dev(tmp_path):
    directory = tmp_path / "adapter"
    (directory / "node_modules" / "kept").mkdir(parents=True)
    (directory / "node_modules" / "kept" / "package.json").write_text('{"version": "1.2.3"}')
    (directory / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "adapter", "version": "1.0.0"},
                    "node_modules/kept": {"version": "1.2.3"},
                    "node_modules/only-dev": {"version": "4.5.6", "dev": True},
                    "node_modules/skipped": {"version": "7.8.9", "optional": True},
                },
            }
        )
    )
    # npm ci --omit=dev correctly installs neither, so neither is a mismatch.
    assert insights._dependencies_match(directory)
    (directory / "node_modules" / "kept" / "package.json").write_text('{"version": "0.0.1"}')
    assert not insights._dependencies_match(directory)


def test_missing_marker_restamps_without_reinstalling(engine, tmp_path, monkeypatch, capsys):
    directory = adapter_copy(tmp_path, node_modules=True)
    monkeypatch.setattr(insights, "ADAPTER", directory)
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[1:2] == ["ci"]:
            raise AssertionError("a complete tree must not reach the registry")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    code, document, error = invoke(capsys, "--setup")
    assert code == 0, error
    assert document["ready"] is True
    assert (directory / ".installed").read_text() == insights._lock_digest(directory)
    assert cli.main(["insights", "--trace", str(FIXTURES / "http-navigation.json")]) == 0


def test_a_deleted_setup_lock_is_recreated(engine, tmp_path, monkeypatch, capsys):
    directory = adapter_copy(tmp_path, node_modules=True, installed=True)
    monkeypatch.setattr(insights, "ADAPTER", directory)
    assert not (directory / ".setup.lock").exists()
    code, document, error = invoke(capsys, "--trace", str(FIXTURES / "http-navigation.json"))
    assert code == 0, error
    assert len(document["navigations"][0]["insights"]) == 19


def test_waiting_behind_a_held_setup_lock_says_so(tmp_path, capsys):
    path = tmp_path / ".setup.lock"
    path.touch()
    holder = path.open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)

    def release():
        time.sleep(0.2)
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    thread = threading.Thread(target=release)
    thread.start()
    with insights._locked(tmp_path, fcntl.LOCK_SH):
        pass
    thread.join()
    assert "waiting for bt insights --setup to release" in capsys.readouterr().err


def test_vendored_sources_keep_the_upstream_notice():
    files = sorted((insights.ADAPTER / "vendor").glob("*.js"))
    assert len(files) == 4
    for path in files:
        assert "Copyright" in path.read_text(), path
