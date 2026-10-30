"""Guards for the login test modules' own support code.

The round-four review (Quality Q8, Reuse, A's nit) found the approved-login
work leaving dead scaffolding behind in the two test modules it had modified:
an orphan private helper method and unused local bindings - a repeat of the
standing dead-code rule. ``F841`` is deliberately ignored in the repository's
Ruff config and Pyright checks only ``src/``, so nothing rejected them.

These two checks close exactly that gap, and only that gap: they enforce
that private helper *methods* and *local variable bindings* in
``tests/test_login_cancellation.py`` and ``tests/test_login_ownership.py``
are consumed. This is not a repository-wide dead-code analysis, and it does
not attempt to prove anything about untested runtime branches; both checks
run from the ordinary ``uv run pytest tests/`` invocation (and therefore
CI), using the standard ``vulture`` analyzer (a dev dependency) and the
repository's existing Ruff, deliberately re-enabled for this one rule on
these two files only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vulture import Vulture

from browser_tools import login as login_module

#: The two login test modules the approved-login work modified, and whose
#: support code these guards keep honest. ``login.py`` joins the scavenge
#: set as the real consumer of the code under test (it calls
#: ``deadline.remaining()`` and everything else the harness exercises), so
#: legitimately-used runtime code is never reported.
TEST_MODULES = (
    "test_login_cancellation.py",
    "test_login_ownership.py",
)


def test_login_test_modules_have_no_unused_private_helper_methods() -> None:
    """Orphan private methods in the login test modules fail, named.

    Vulture's standard analyzer reads the two test modules plus their
    consumer ``src/browser_tools/login.py``; only *methods* (code defined
    inside a class) with a single leading underscore in one of the two
    test modules are reported. No handwritten call graph and no blanket
    name whitelist: a private helper is kept alive only by an actual
    reference in the analyzed set.
    """
    tests = Path(__file__).parent
    scavenge = [str(tests / name) for name in TEST_MODULES]
    scavenge.append(str(Path(login_module.__file__).resolve()))
    vulture = Vulture()
    vulture.scavenge(scavenge)
    orphans = sorted(
        f"{Path(item.filename).name}:{item.first_lineno}: {item.name}"
        for item in vulture.get_unused_code()
        if item.typ == "method"
        and Path(item.filename).name in TEST_MODULES
        and item.name.startswith("_")
        and not item.name.startswith("__")
    )
    detail = ", ".join(orphans)
    assert not orphans, (
        f"unused private helper method(s) in the login test modules ({detail}): "
        "the change left dead test support behind. "
        "Delete the unused helper, or - if it is setup the tests still need - "
        "move it into a fixture the tests actually consume"
    )


def test_login_test_modules_have_no_unused_local_bindings() -> None:
    """Unused local variable bindings in the login test modules fail.

    Runs the repository's own Ruff (through the current interpreter) with
    ``--isolated --select F841`` on the same two test modules: the global
    ``F841`` ignore stays untouched for the rest of the repository, and
    this one scoped invocation still names every unused local in these
    two files. An awaited call or other needed side effect stays; only
    the unused *assignment* is rejected.
    """
    tests = Path(__file__).parent
    completed = subprocess.run(
        [
            sys.executable, "-m", "ruff",
            "check", "--isolated", "--select", "F841",
            *(str(tests / name) for name in TEST_MODULES),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        "unused local variable binding(s) in the login test modules:\n"
        f"{completed.stdout}{completed.stderr}"
        "Remove the unused assignment, keeping the awaited call or other "
        "needed side effect (assign to the name only when it is read, or "
        "drop the name entirely); never delete an assertion or a wait"
    )
