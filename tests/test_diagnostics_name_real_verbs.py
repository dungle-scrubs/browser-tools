"""Every remedy a diagnostic offers must name something a caller can actually run.

RFC-01, "Refusals and exit codes": each refusal "MUST name the remedy in its
diagnostic, and the named remedy MUST work." The CLI is the only surface (the MCP
front went in RFC-01 Phase 5), so a message that tells the caller to run an
internal tool name is naming a remedy that does not exist.

Two shipped examples motivated this guard, both found while drafting RFC-03:

- ``get_frame_storage`` said "Use select_frame first". The verb is ``frames select``.
- ``screencast_start`` said "call screencast_stop first". RFC-01 removed the
  start/stop pair; there is no such verb, by design.

The check is on the *remedy* phrasing, not on any mention. A message may name a
tool as its subject, as ``ax_find requires at least one of 'role' or 'name'`` does,
because that is not an instruction to go and run something.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from browser_tools.tool_registry import TOOLS

SRC = Path(__file__).resolve().parent.parent / "src" / "browser_tools"

#: Verbs the parser owns, mirrored from the CLI. A remedy may name one of these.
#: Kept as a literal on purpose: the point is to catch a message naming something
#: that is NOT here.
RESPONSE_BUILDERS = ("make_error", "make_text")

#: "call X", "use X", "run X", "X first" -- an instruction to go and do something.
REMEDY = re.compile(
    r"\b(?:call|use|run|try)\s+[`'\"]?([a-z_][a-z0-9_]*)\b", re.IGNORECASE
)


def _messages() -> list[tuple[str, int, str]]:
    """Every literal string passed to a response builder, with its location."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) not in RESPONSE_BUILDERS:
                continue
            parts = [
                sub.value
                for arg in node.args
                for sub in ast.walk(arg)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            ]
            if parts:
                found.append((path.name, node.lineno, " ".join(parts)))
    return found


def test_no_diagnostic_offers_an_internal_tool_name_as_a_remedy() -> None:
    offenders: list[str] = []
    for filename, lineno, message in _messages():
        for named in REMEDY.findall(message):
            if named in TOOLS:
                offenders.append(f"{filename}:{lineno} tells the caller to run {named!r}: {message!r}")
    assert not offenders, (
        "a diagnostic names an internal tool as the remedy; name the CLI verb "
        "instead (RFC-01, 'Refusals and exit codes'):\n  " + "\n  ".join(offenders)
    )


def test_the_guard_sees_the_messages_it_is_meant_to_guard() -> None:
    """A regex that matched nothing would pass the test above for the wrong reason."""
    messages = _messages()
    assert len(messages) > 20, f"only found {len(messages)} diagnostics; the AST walk is not working"
    assert any("No frame selected" in m for _, _, m in messages)
