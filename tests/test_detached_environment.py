"""A detached process must not inherit the launching terminal's identity (#213).

Herdr's terminal core gives every terminal identity variables
(``HC_TERMINAL_ID``, ``HC_SCOPE``, ``HC_TERMINAL_SOCKET`` and the ``HERDR_*``
family) and ends a terminal by signalling every process whose environment
carries them -- including processes that left the terminal's session with
``start_new_session=True``. browser-tools passed its whole environment to the
processes it detaches, so a supervisor launched from a Herdr terminal died with
that terminal and its browser ran on unsupervised.

These tests cover the helper that strips those variables and the one spawn
site that has no test module of its own: the detached Camoufox runner.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from browser_tools import camoufox_runner, lifecycle
from browser_tools.spawn_environment import TERMINAL_IDENTITY_PREFIXES, detached_environment

if TYPE_CHECKING:
    import pytest

#: The environment a Herdr terminal hands to the processes it launches. The
#: values are synthetic; nothing real reaches an assertion message.
IDENTITY_ENVIRONMENT: dict[str, str] = {
    "BROWSER_TOOLS_TEST_ENV": "synthetic",
    "HC_TERMINAL_ID": "t9",
    "HC_SCOPE": "synthetic-scope",
    "HC_TERMINAL_SOCKET": "/tmp/synthetic.sock",
    "HERDR_PANE_ID": "synthetic-pane",
}

#: What a detached process must be left with.
SURVIVING_ENVIRONMENT: dict[str, str] = {"BROWSER_TOOLS_TEST_ENV": "synthetic"}


class TestDetachedEnvironment:
    """The helper that strips the terminal's identity variables."""

    def test_identity_variables_are_removed(self) -> None:
        """A variable naming the terminal must not reach a detached process."""
        result = detached_environment(IDENTITY_ENVIRONMENT)

        assert "HC_TERMINAL_ID" not in result
        assert "HC_SCOPE" not in result
        assert "HC_TERMINAL_SOCKET" not in result
        assert "HERDR_PANE_ID" not in result

    def test_other_variables_keep_their_names_and_values(self) -> None:
        """Only the identity goes; everything else is the process's own."""
        result = detached_environment(
            {"BROWSER_TOOLS_TEST_ENV": "synthetic", "PATH": "/usr/bin:/bin"}
        )

        assert result == {"BROWSER_TOOLS_TEST_ENV": "synthetic", "PATH": "/usr/bin:/bin"}

    def test_the_mapping_passed_in_is_not_modified(self) -> None:
        """The caller's environment must survive the call untouched."""
        mapping: dict[str, str] = dict(IDENTITY_ENVIRONMENT)

        detached_environment(mapping)

        assert mapping == IDENTITY_ENVIRONMENT

    def test_a_name_that_merely_contains_the_prefix_is_kept(self) -> None:
        """Only a prefix at the start of the name marks a terminal variable."""
        result = detached_environment({"MY_HC_VALUE": "synthetic"})

        assert result == {"MY_HC_VALUE": "synthetic"}

    def test_the_prefixes_are_the_terminal_identity_families(self) -> None:
        """The stripped families are Herdr's own, and they are named here."""
        assert TERMINAL_IDENTITY_PREFIXES == ("HC_", "HERDR_")


class TestTheCamoufoxRunnerEnvironment:
    """The detached Camoufox runner starts without the terminal's identity."""

    def test_the_runner_starts_without_the_launching_terminals_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        class _FakeProcess:
            pid = 424242
            returncode: int | None = None

            def poll(self) -> None:
                return None

        def fake_popen(args, **kwargs):
            captured["env"] = kwargs.get("env")
            # The runner signals readiness by writing a sentinel file; the
            # double writes it so the call does not wait for a real browser.
            user_data_dir = next(
                arg.split("=", 1)[1]
                for arg in args
                if arg.startswith("--user-data-dir=")
            )
            (Path(user_data_dir) / camoufox_runner.READY_SENTINEL).write_text("ok")
            return _FakeProcess()

        # Only the runner's own spawn is doubled; the module's other uses of
        # the subprocess module (the start-time probe) stay real.
        monkeypatch.setattr(
            lifecycle,
            "subprocess",
            SimpleNamespace(
                Popen=fake_popen, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE
            ),
        )
        monkeypatch.setattr(os.environ, "copy", lambda: dict(IDENTITY_ENVIRONMENT))

        lifecycle._spawn_camoufox_process(tmp_path, headless=False)

        assert captured["env"] == SURVIVING_ENVIRONMENT
