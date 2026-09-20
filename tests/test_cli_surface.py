"""The CLI is the only surface, and its import closure proves it (RFC-01 #44, #92).

The MCP front and the legacy session stack behind it are gone. What used to be
a promise -- that a CLI verb could not start a daemon because the supervisor was
never imported -- is now a fact about the tree, and these tests pin both halves
of it:

1. **The removed stack is unimportable.** Every deleted module raises
   ``ModuleNotFoundError``. A ``sys.modules`` check alone would pass vacuously
   against a module that no longer exists, so the deletion is asserted first and
   the closure second.
2. **The closure is lean.** A fresh interpreter that imports the CLI path, or
   the profiler, or the bare package, loads none of them -- and the package's
   lazy re-exports name only modules that exist, so no attribute access can
   raise ``ModuleNotFoundError`` at runtime.
3. **Camoufox stays cold.** Importing the lifecycle layer does not load the
   Camoufox runner; only a Camoufox launch does. The runner is the one
   remaining path into the optional extra.
4. **Live dispatch.** The daemonless verbs run through ``cli.main`` against an
   isolated registry and return the expected exit codes.

Every import assertion runs in a *fresh* interpreter, so it is about what the
import pulls in, not what the pytest process happens to have loaded already.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from browser_tools import cli, lifecycle

# The MCP front and the legacy session stack it drove, deleted in #92. None may
# be importable, and none may appear in any retained module's import closure.
REMOVED_MODULES = [
    "browser_tools.automation_backend",
    "browser_tools.browser_session",
    "browser_tools.browser_state",
    "browser_tools.camoufox_session",
    "browser_tools.chrome_config",
    "browser_tools.chrome_utils",
    "browser_tools.core.session",
    "browser_tools.daemon_client",
    "browser_tools.daemon_supervisor",
    "browser_tools.live_chrome",
    "browser_tools.mcp_broker",
    "browser_tools.mcp_daemon",
    "browser_tools.mcp_session",
    "browser_tools.page_selection",
    "browser_tools.persistent_browser",
    "browser_tools.profile_catalog",
    "browser_tools.project_identity",
    "browser_tools.session_layout",
    "browser_tools.session_reaper",
    "browser_tools.session_store",
]

# The modules the CLI front dispatches through (see cli.py).
_CLI_PATH_IMPORT = (
    "import browser_tools.cli\n"
    "import browser_tools.lifecycle\n"
    "import browser_tools.events\n"
    "import browser_tools.passthrough\n"
    "import browser_tools.list_verbs\n"
    "import browser_tools.curated\n"
)


def _in_fresh_process(script: str) -> str:
    """Run ``script`` in a fresh interpreter and return its last stdout line."""
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip().splitlines()[-1]


def _loaded_after(preamble: str, watched: list[str]) -> list[str]:
    """Names from ``watched`` present in ``sys.modules`` after ``preamble`` runs."""
    script = (
        preamble + "import json, sys\n"
        f"watched = {watched!r}\n"
        "print(json.dumps([m for m in watched if m in sys.modules]))\n"
    )
    return json.loads(_in_fresh_process(script))


class TestTheRemovedStackIsGone:
    """Deleted means unimportable, not merely unimported."""

    @pytest.mark.parametrize("module", REMOVED_MODULES)
    def test_module_is_unimportable(self, module: str) -> None:
        script = (
            "import importlib, sys\n"
            f"try:\n    importlib.import_module({module!r})\n"
            "    print('IMPORTED')\n"
            "except ModuleNotFoundError:\n    print('GONE')\n"
        )
        assert _in_fresh_process(script) == "GONE", f"{module} is still importable"


class TestTheImportClosureIsLean:
    """No retained entry point reaches the removed stack."""

    def test_cli_import_reaches_nothing_removed(self) -> None:
        present = _loaded_after(_CLI_PATH_IMPORT, REMOVED_MODULES)
        assert present == [], f"the CLI import path pulled in: {present}"

    def test_profiler_import_reaches_nothing_removed(self) -> None:
        present = _loaded_after("import browser_tools.profiler\n", REMOVED_MODULES)
        assert present == [], f"the profiler import pulled in: {present}"

    def test_top_level_package_import_reaches_nothing_removed(self) -> None:
        present = _loaded_after("import browser_tools\n", REMOVED_MODULES)
        assert present == [], f"the package import pulled in: {present}"

    def test_no_lazy_export_names_a_removed_module(self) -> None:
        """A lazy attribute pointing at a deleted module fails only on access."""
        script = (
            "import browser_tools, json\n"
            "print(json.dumps(sorted(set(browser_tools._LAZY_EXPORTS.values()))))\n"
        )
        targets = json.loads(_in_fresh_process(script))
        removed = {m.rsplit(".", 1)[-1] for m in REMOVED_MODULES}
        named = {t.lstrip(".") for t in targets} & removed
        assert not named, f"lazy exports still name deleted modules: {sorted(named)}"

    def test_every_lazy_export_resolves(self) -> None:
        """The other half: each re-exported name still resolves on access."""
        script = (
            "import browser_tools, json\n"
            "print(json.dumps([n for n in browser_tools._LAZY_EXPORTS "
            "if getattr(browser_tools, n, None) is None]))\n"
        )
        assert json.loads(_in_fresh_process(script)) == []


class TestCamoufoxStaysCold:
    """The optional extra loads on a Camoufox launch, not before."""

    def test_lifecycle_import_does_not_load_the_runner(self) -> None:
        present = _loaded_after(
            "import browser_tools.lifecycle\n", ["browser_tools.camoufox_runner"]
        )
        assert present == [], "importing lifecycle loaded the Camoufox runner"

    def test_cli_import_does_not_load_the_runner(self) -> None:
        present = _loaded_after(_CLI_PATH_IMPORT, ["browser_tools.camoufox_runner"])
        assert present == [], "the CLI import path loaded the Camoufox runner"


class TestDaemonlessVerbs:
    """Every verb dispatches to a well-formed result with nothing else running."""

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
