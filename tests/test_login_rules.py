"""Guards for the login test modules' own support code and size.

The round-four review (Quality Q8, Reuse, A's nit) found the approved-login
work leaving dead scaffolding behind in the two test modules it had modified:
an orphan private helper method and unused local bindings - a repeat of the
standing dead-code rule. ``F841`` is deliberately ignored in the repository's
Ruff config and Pyright checks only ``src/``, so nothing rejected them.

The round-five review (Quality Q10) found the same class of unenforced
written rule one level up: ``tests/test_login_live.py`` had grown past
CONTRIBUTING.md's module-size guidance (~800 lines) with nothing rejecting
an oversized module.

These checks close exactly those gaps, and only those gaps: the first two
enforce that private helper *methods* and *local variable bindings* in
``tests/test_login_cancellation.py`` and ``tests/test_login_ownership.py``
are consumed, and the third enforces the size limit on the two whole-flow
login modules, ``tests/test_login_live.py`` and ``tests/test_login_bridge.py``
(the round-six Q10 extraction). This is not a repository-wide dead-code or
size analysis, and it does not attempt to prove anything about untested
runtime branches; the first two checks run from the ordinary
``uv run pytest tests/`` invocation (and therefore CI), using the standard
``vulture`` analyzer (a dev dependency) and the repository's existing Ruff,
deliberately re-enabled for this one rule on these two files only, and the
size check simply reads the two modules' actual line counts.
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


#: The two whole-flow login test modules kept under the size limit, oldest
#: first: the limit is checked against the actual files (so a missing module
#: is itself a failure), and the long-lived ``test_login_live.py`` is named
#: first so an oversized live module is reported for what it is - extraction
#: overdue - rather than as a missing ``test_login_bridge.py``.
SIZE_MODULES = (
    "test_login_live.py",
    "test_login_bridge.py",
)

#: CONTRIBUTING.md's module-size guidance: modules stay under ~800 lines.
SIZE_LIMIT = 800


def test_login_test_modules_stay_under_the_module_size_limit() -> None:
    """A login test module over the size limit fails, named with its count.

    Reads the actual current files rather than any snapshot, so the guard
    holds for every future change to the two whole-flow login modules: an
    oversized one is reported with its real line count and the instruction
    CONTRIBUTING.md already gives - extract a cohesive set of symbols into
    a new test module - never a license to delete a case to fit the limit.
    """
    tests = Path(__file__).parent
    for name in SIZE_MODULES:
        path = tests / name
        assert path.is_file(), (
            f"tests/{name} does not exist; the whole-flow login tests are "
            "organized as exactly these two modules"
        )
        count = len(path.read_text(encoding="utf-8").splitlines())
        assert count <= SIZE_LIMIT, (
            f"tests/{name} is {count} lines, over the {SIZE_LIMIT}-line "
            "module-size limit (CONTRIBUTING.md, Module Size): extract a "
            "cohesive set of symbols into a new test module and migrate "
            "the references to it, keeping every collected case exactly "
            "once (do not re-export test classes; pytest would collect "
            "them twice)"
        )
