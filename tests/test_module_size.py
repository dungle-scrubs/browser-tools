"""Every module under ``src/`` and ``tests/`` stays within 800 lines.

CONTRIBUTING.md's module-size rule was enforced by three hand-picked rule
modules, each guarding only the files one review round had touched. This
check enforces it everywhere instead: every Python module under ``src/`` and
``tests/`` is measured, and a module over the limit has to be listed in
``EXCEPTIONS`` with a ceiling that only goes down.

The list records debt. It is not a licence to keep growing: a module that
shrinks back under the limit is removed from it, and a module that needs
more room is split rather than given a higher ceiling.
"""

from __future__ import annotations

from pathlib import Path

#: The module-size limit CONTRIBUTING.md states.
LIMIT = 800

#: The modules over the limit, mapped to the ceiling each one is held to.
#: This dict records debt, and it must only shrink: a module that comes back
#: under the limit is removed from it, and a module that grows is split
#: rather than given a higher ceiling.
EXCEPTIONS: dict[str, int] = {
    "src/browser_tools/cdp_handler.py": 2336,
    "src/browser_tools/cli.py": 1671,
    "src/browser_tools/core/supervisor.py": 1115,
    "src/browser_tools/curated.py": 1010,
    "src/browser_tools/frame_manager.py": 816,
    "src/browser_tools/lifecycle.py": 2192,
    "tests/test_chrome_attach.py": 1097,
    "tests/test_curated_verbs.py": 1490,
    "tests/test_guide.py": 901,
    "tests/test_new_tools.py": 921,
    "tests/test_session_hygiene.py": 1230,
    "tests/test_session_root_mutex.py": 808,
    "tests/test_step_run.py": 1315,
}

#: The repository root, one level above ``tests/``.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _python_modules() -> list[Path]:
    """Every Python module under ``src/`` and ``tests/``, sorted."""
    return sorted(
        path
        for base in ("src", "tests")
        for path in (REPO_ROOT / base).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _line_count(path: Path) -> int:
    """The module's line count, as CONTRIBUTING.md counts it."""
    return len(path.read_text(encoding="utf-8").splitlines())


def test_no_module_exceeds_the_size_limit() -> None:
    """A module over the limit fails unless it is listed in ``EXCEPTIONS``."""
    over = [
        f"{path.relative_to(REPO_ROOT).as_posix()} is {_line_count(path)} lines"
        for path in _python_modules()
        if path.relative_to(REPO_ROOT).as_posix() not in EXCEPTIONS
        and _line_count(path) > LIMIT
    ]
    assert not over, (
        f"module(s) over the {LIMIT}-line limit (CONTRIBUTING.md, Module Size): "
        + "; ".join(over)
        + ". Extract a cohesive set of symbols into a new module and move "
        "every reference; leave no re-export."
    )


def test_listed_modules_stay_under_their_ceiling() -> None:
    """A listed module over its own ceiling fails; the ceiling only goes down."""
    over = [
        f"{relative} is {_line_count(REPO_ROOT / relative)} lines, over its "
        f"{ceiling}-line ceiling"
        for relative, ceiling in EXCEPTIONS.items()
        if (REPO_ROOT / relative).is_file()
        and _line_count(REPO_ROOT / relative) > ceiling
    ]
    assert not over, (
        "module(s) over their recorded ceiling: "
        + "; ".join(over)
        + ". The ceiling only goes down; split the module."
    )


def test_the_exception_list_only_records_current_debt() -> None:
    """A listed module that is gone or back under the limit must be unlisted."""
    stale = [
        f"{relative} does not exist"
        for relative in EXCEPTIONS
        if not (REPO_ROOT / relative).is_file()
    ]
    stale += [
        f"{relative} is {_line_count(REPO_ROOT / relative)} lines, at or under "
        f"the {LIMIT}-line limit"
        for relative in EXCEPTIONS
        if (REPO_ROOT / relative).is_file() and _line_count(REPO_ROOT / relative) <= LIMIT
    ]
    assert not stale, (
        "module(s) in EXCEPTIONS that no longer need an exception: "
        + "; ".join(stale)
        + ". Remove it from EXCEPTIONS."
    )
