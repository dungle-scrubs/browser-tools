"""Pin the agent skill's documented verb list to the real CLI (RFC-01 #48).

The skill at ``skills/browser-tools/SKILL.md`` is the CLI-first surface's
documentation. RFC-01 Phase 4 requires the skill's documented verbs to match
``browser-tools --help`` exactly, so an agent reading the skill never sees a
verb the CLI does not have (or misses one it does).

The matching scheme is deliberately machine-checkable: the skill carries a
fenced code block introduced by ```verbs`` that lists one top-level verb per
line. This test parses that block and asserts it equals the CLI's real
top-level verbs, derived from ``browser_tools.cli.build_parser()`` (the same
parser that produces ``--help``). If a verb is added, removed, or renamed in
the CLI, this test fails until the block is updated -- the skill cannot silently
drift.

The passthrough forms (``INSTANCE Domain.method`` and the ``help`` disambiguated
head) are intentionally out of scope: they are not argparse subcommands and do
not appear in the CLI's top-level ``--help`` verb list.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from browser_tools import cli

_SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "browser-tools" / "SKILL.md"

_VERBS_BLOCK = re.compile(r"```verbs\n(.*?)```", re.DOTALL)


def _documented_verbs() -> list[str]:
    """Parse the machine-checkable ``verbs`` block from the skill."""
    text = _SKILL_PATH.read_text()
    matches = _VERBS_BLOCK.findall(text)
    assert matches, "SKILL.md must contain exactly one ```verbs fenced block"
    assert len(matches) == 1, "SKILL.md must contain exactly one ```verbs fenced block"
    lines = [line.strip() for line in matches[0].splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _cli_verbs() -> set[str]:
    """The CLI's real top-level verbs, straight from the argparse parser."""
    parser = cli.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices.keys())


def test_skill_file_exists() -> None:
    assert _SKILL_PATH.is_file(), f"skill missing at {_SKILL_PATH}"


def test_documented_verbs_match_cli_help() -> None:
    documented = _documented_verbs()
    # No duplicates in the documented block.
    assert len(documented) == len(set(documented)), "duplicate verb in SKILL.md verbs block"
    assert set(documented) == _cli_verbs()


def test_documented_verbs_are_actually_accepted() -> None:
    """Each documented verb parses as a real subcommand (belt-and-braces)."""
    parser = cli.build_parser()
    for verb in _documented_verbs():
        args = parser.parse_args([verb] if verb != "wait" else [verb, "--event", "X"])
        assert args.command == verb
