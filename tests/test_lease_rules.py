"""Enforce the written rules the idle-expiry change (#195) first broke.

CONTRIBUTING.md states each rule. Its ordinary checks did not catch these
three in the new modules, so each has a scoped check here, the way
``tests/test_onepassword_rules.py`` guards the #201 modules.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from browser_tools import idle, lease_cli, task_lease

TESTS = Path(__file__).parent

#: Modules above ``idle`` in the layering. ``idle`` holds the duration
#: grammar and the deadline arithmetic, and imports none of them.
ABOVE_IDLE = ("lifecycle", "task_lease", "lease_cli", "cli")

_LAYERING_SCRIPT = (
    "import sys; import browser_tools.idle; "
    "loaded = [name for name in sys.argv[1:] if 'browser_tools.' + name in sys.modules]; "
    "assert not loaded, loaded"
)


def _imports_of(source: str, names: tuple[str, ...]) -> list[str]:
    """Find every import of the named package modules, at any depth.

    Args:
        source: The source of a module directly inside ``browser_tools``.
        names: The sibling module names to look for.

    Returns:
        ``<name> at line <n>`` for each import, a function-local one included.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported = [alias.name.removeprefix("browser_tools.") for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and (
            node.level == 1 or (node.module or "").startswith("browser_tools")
        ):
            module = (node.module or "").removeprefix("browser_tools").strip(".")
            imported = [module.split(".")[0]] if module else [alias.name for alias in node.names]
        else:
            continue
        found += [f"{name} at line {node.lineno}" for name in imported if name in names]
    return found


def test_the_idle_module_imports_nothing_above_it() -> None:
    """``idle`` stays a leaf, so the lease modules can import it without a cycle.

    A function-local import defers a cycle without removing it, so the source
    scan refuses one at any depth, and a fresh interpreter confirms that
    importing ``idle`` loads none of the modules above it.
    """
    found = _imports_of(Path(inspect.getfile(idle)).read_text(encoding="utf-8"), ABOVE_IDLE)
    assert not found, (
        "browser_tools.idle imports a module above it: " + ", ".join(found) + " "
        "(CONTRIBUTING.md: no circular imports). Keep durations and deadlines in "
        "idle.py and put lease and registry work in task_lease.py."
    )
    result = subprocess.run(
        [sys.executable, "-c", _LAYERING_SCRIPT, *ABOVE_IDLE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "importing browser_tools.idle loaded a module above it (CONTRIBUTING.md: "
        "no circular imports).\n" + result.stderr
    )


@pytest.mark.parametrize(
    ("function", "sections"),
    [
        (idle.parse_duration, ("Args:", "Returns:", "Raises:")),
        (idle.iso_utc, ("Args:", "Returns:")),
        (idle.deadline_of, ("Args:", "Returns:")),
        (lease_cli.add_launch_timeout, ("Args:",)),
        (lease_cli.add_configure_verb, ("Args:",)),
        (lease_cli.check_timeout, ("Args:", "Raises:")),
        (lease_cli.configure_payload, ("Args:", "Returns:", "Raises:")),
        (lease_cli.launch_fields, ("Args:", "Returns:")),
        (task_lease.load_registry, ("Args:", "Returns:", "Raises:")),
        (task_lease.save_registry, ("Args:",)),
        (task_lease.status, ("Args:", "Returns:")),
        (task_lease.summary, ("Args:", "Returns:", "Raises:")),
        (task_lease.establish, ("Args:", "Raises:")),
        (task_lease.configure, ("Args:", "Returns:", "Raises:")),
        (task_lease.watch_once, ("Args:", "Returns:", "Raises:")),
        (task_lease.process_identity, ("Returns:",)),
        (task_lease.begin, ("Args:",)),
    ],
)
def test_lease_boundaries_have_google_style_sections(
    function: object, sections: tuple[str, ...]
) -> None:
    """Keep the idle-expiry boundary documentation in CONTRIBUTING's format."""
    doc = inspect.getdoc(function) or ""
    name = getattr(function, "__qualname__", repr(function))
    for section in sections:
        assert section in doc.splitlines(), (
            f"{name} is missing {section} from its Google-style docstring "
            "(CONTRIBUTING.md). Document the boundary's arguments, result and failures."
        )


def _unannotated(path: Path, only: tuple[str, ...] | None = None) -> list[str]:
    """Name each module-level function with a missing annotation.

    Args:
        path: The test module to scan.
        only: The function names to check, or None for every one.

    Returns:
        ``<function>: <what is missing>`` for each incomplete signature.
    """
    problems: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if only is not None and node.name not in only:
            continue
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        missing = [arg.arg for arg in arguments if arg.annotation is None]
        if node.returns is None:
            missing.append("return")
        if missing:
            problems.append(f"{node.name}: {', '.join(missing)}")
    return problems


@pytest.mark.parametrize(
    ("module", "only"),
    [
        ("test_idle_expiry.py", None),
        ("idle_fixtures.py", None),
        ("test_lease_rules.py", None),
        ("conftest.py", ("keeper_double_by_default", "real_keeper")),
    ],
)
def test_idle_expiry_tests_and_fixtures_are_fully_annotated(
    module: str, only: tuple[str, ...] | None
) -> None:
    """Every parameter and return of the #195 tests and fixtures is typed."""
    problems = _unannotated(TESTS / module, only)
    assert not problems, (
        f"tests/{module} has unannotated functions (CONTRIBUTING.md: type "
        "annotations required on all public functions). Annotate each parameter "
        "with its fixture's type and give a truthful return type:\n" + "\n".join(problems)
    )
    if only is not None:
        defined = {
            node.name
            for node in ast.parse((TESTS / module).read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef)
        }
        gone = sorted(set(only) - defined)
        assert not gone, f"tests/{module} no longer defines {gone}"
