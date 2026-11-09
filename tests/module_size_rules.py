"""One owner for CONTRIBUTING.md's module-size check.

CONTRIBUTING.md keeps modules under ~800 lines, and three rule modules
enforce it: ``tests/test_onepassword_rules.py`` (the #201 source modules and
the 1Password lifecycle test modules) and ``tests/test_login_rules.py`` (the
whole-flow login test modules). Each of the three checks repeated the same
steps - the module file must exist, its line count is read, and the count is
compared with the limit - so a fix to one copy could miss the others (#216).

This module owns that check once. Every caller passes the directory, the
ordered module names, the path prefix its failure messages use, the
missing-module sentence and the oversize remedy, so each rule module keeps
its own wording, its own scope and its own order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: CONTRIBUTING.md's module-size guidance: modules stay under ~800 lines.
SIZE_LIMIT = 800


def assert_modules_within_size_limit(
    directory: Path,
    module_names: Sequence[str],
    *,
    path_prefix: str,
    missing_message: str,
    oversize_remedy: str,
) -> None:
    """Assert every named module exists and stays within the size limit.

    Args:
        directory: The directory the module names are relative to.
        module_names: The module file names, in the order they are checked.
        path_prefix: The path prefix the failure messages use, for example
            ``"tests/"``.
        missing_message: The sentence explaining what the modules are
            organized as, used when one of them is missing.
        oversize_remedy: The remedy sentence used when one of them is over
            the limit.

    Raises:
        AssertionError: When a module is missing, or when a module is over
            the size limit.
    """
    for name in module_names:
        path = directory / name
        assert path.is_file(), f"{path_prefix}{name} does not exist; {missing_message}"
        count = len(path.read_text(encoding="utf-8").splitlines())
        assert count <= SIZE_LIMIT, (
            f"{path_prefix}{name} is {count} lines, over the {SIZE_LIMIT}-line "
            f"module-size limit (CONTRIBUTING.md, Module Size): {oversize_remedy}"
        )
