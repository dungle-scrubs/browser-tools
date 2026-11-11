"""Guards for the login test modules' own support code.

The round-four review (Quality Q8, Reuse, A's nit) found the approved-login
work leaving dead scaffolding behind in the two test modules it had modified:
an orphan private helper method and unused local bindings - a repeat of the
standing dead-code rule. The repository's Ruff now rejects an unused local
binding everywhere, so only the private helper *methods* need a check here.

This check enforces that private helper methods in
``tests/test_login_cancellation.py`` and ``tests/test_login_ownership.py``
are consumed. It is not a repository-wide dead-code analysis, and it does
not attempt to prove anything about untested runtime branches; it runs from
the ordinary ``uv run pytest tests/`` invocation (and therefore CI), using
the standard ``vulture`` analyzer (a dev dependency).
"""

from __future__ import annotations

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


