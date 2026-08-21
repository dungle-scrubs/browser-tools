"""Prove the MCP front is optional: every CLI verb runs with no daemon (RFC-01 #44).

Part A of the daemon-demotion ticket commits that nothing in the core or any CLI
verb path requires a running daemon. These tests pin that at two levels:

1. Import closure -- importing the CLI (and every module on the verb-dispatch
   path) drags in none of the MCP front / legacy session stack: the daemon
   broker, its supervisor and client, the persistent controller, the session
   store/reaper, or the Camoufox session. Because the supervisor that spawns a
   daemon is never even imported on this path, a CLI verb categorically cannot
   start one. A regression that couples the CLI to the daemon fails this test.

2. Live dispatch -- running the daemonless verbs through ``cli.main`` against an
   isolated registry returns the expected exit codes with no daemon alive.

The import-closure half runs in a *fresh* interpreter (subprocess) so the
assertion is about what the CLI import pulls in, not what the pytest process
happens to have loaded from other tests.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from browser_tools import cli, lifecycle

# Modules that make up the optional MCP front and the legacy session stack it
# drives. None of them may be imported as a side effect of loading the CLI.
FORBIDDEN_MODULES = [
    "browser_tools.mcp_daemon",
    "browser_tools.mcp_broker",
    "browser_tools.daemon_supervisor",
    "browser_tools.daemon_client",
    "browser_tools.persistent_browser",
    "browser_tools.session_store",
    "browser_tools.session_reaper",
    "browser_tools.browser_session",
    "browser_tools.automation_backend",
    "browser_tools.mcp_session",
    "browser_tools.camoufox_session",
    "browser_tools.camoufox_runner",
]

# The modules the CLI front dispatches through (see cli.py). Importing all of
# them must still not load the daemon stack.
_CLI_PATH_IMPORT = (
    "import browser_tools.cli\n"
    "import browser_tools.lifecycle\n"
    "import browser_tools.events\n"
    "import browser_tools.passthrough\n"
    "import browser_tools.list_verbs\n"
)


def _forbidden_present(preamble: str) -> list[str]:
    """Run ``preamble`` in a fresh interpreter; return loaded forbidden modules."""
    script = (
        preamble + "import json, sys\n"
        f"forbidden = {FORBIDDEN_MODULES!r}\n"
        "print(json.dumps([m for m in forbidden if m in sys.modules]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_cli_import_pulls_in_no_daemon_stack():
    """A fresh interpreter that imports the CLI path loads no daemon module."""
    present = _forbidden_present(_CLI_PATH_IMPORT)
    assert present == [], f"CLI import path pulled in daemon/session modules: {present}"


def test_top_level_package_import_is_daemon_free():
    """Bare ``import browser_tools`` loads none of the daemon stack (lazy init)."""
    present = _forbidden_present("import browser_tools\n")
    assert present == [], f"package import pulled in daemon/session modules: {present}"


class TestDaemonlessVerbs:
    """Every daemonless verb dispatches to a well-formed result with no daemon."""

    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch, tmp_path):
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, str(tmp_path / "registry.json"))

    def test_status_empty(self, capsys):
        assert cli.main(["status"]) == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == []

    def test_cleanup(self, capsys):
        assert cli.main(["cleanup"]) == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {"removed": []}

    def test_guide(self, capsys):
        assert cli.main(["guide"]) == cli.EXIT_OK
        assert "launch" in capsys.readouterr().out

    def test_stop_unknown_is_operational(self, capsys):
        assert cli.main(["stop", "ghost"]) == cli.EXIT_OPERATIONAL
        assert "error:" in capsys.readouterr().err

    def test_no_verb_is_usage(self):
        assert cli.main([]) == cli.EXIT_USAGE
