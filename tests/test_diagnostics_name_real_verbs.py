"""A user-facing diagnostic must not offer a command the caller cannot run.

RFC-01, "Refusals and exit codes": each refusal "MUST name the remedy in its
diagnostic, and the named remedy MUST work." The CLI is the only surface since the
MCP front went in Phase 5, so a message mentioning an internal tool name, or
upstream's program name, offers something that does not exist.

Four shipped examples motivated this guard, all found while drafting RFC-03:

- ``get_frame_storage`` said "Use select_frame first". The verb is ``frames select``.
  Worse, it then said to run ``frames select PATTERN`` first, which also fails:
  selection dies with the process, so a separate invocation cannot prepare a later
  ``storage get``. Only ``--key`` works.
- ``select_frame`` said "Use list_frames". The verb is ``frames list``.
- ``screencast_start`` and ``screencast_stop`` pointed at each other. RFC-01:43
  removed that pair.
- ``take_snapshot`` and ``chrome-agent launch`` name a tool and a program that this
  package does not install.

WHAT THIS GUARD COVERS, EXACTLY
-------------------------------
It is a vocabulary check, and vocabulary is not behavior. It reads string literals
and asks whether a forbidden name appears. It does **not** execute a suggested
command, so it cannot prove a remedy works; the ``frames select`` case above passed
an earlier vocabulary-only version of this guard while still being broken. Behavior
is covered by the per-verb tests, not here.

Collected: literals passed to ``make_error`` and ``make_text``, and to
``LifecycleError``, ``UsageError`` and their local subclasses, positionally or by
keyword, across ``src/browser_tools`` recursively.

Not collected: text built into a variable first, f-string parts supplied at
runtime, messages forwarded from the browser, and argparse's own output. Those are
out of reach of a static read.

``core/`` is excluded because RFC-01 vendors it verbatim. Its one offending message
is adapted at the call sites instead, which this file checks separately.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from browser_tools.tool_registry import TOOLS

SRC = Path(__file__).resolve().parent.parent / "src" / "browser_tools"

BUILDERS = frozenset({"make_error", "make_text"})
RAISERS = frozenset({"LifecycleError", "UsageError", "PassthroughUsageError", "HiddenTargetError"})

#: Internal tool names and upstream program names. None is a CLI verb, so none can
#: appear in a diagnostic a caller reads. Derived from the tool registry, plus the
#: names that are not in it but still leaked into messages.
FORBIDDEN: frozenset[str] = frozenset(TOOLS) | frozenset(
    {"take_snapshot", "click_uid", "fill_uid", "chrome-agent", "chrome_agent"}
)

#: Messages allowed to contain a forbidden name, each with the reason. A name is
#: permitted only where it is the subject of the sentence, never the remedy.
#: Adding a row here is a deliberate act; the guard fails closed without one.
ALLOWED: tuple[tuple[str, str], ...] = (
    (
        "ax_find requires at least one of",
        "names the tool as the subject of its own argument error, not as a remedy",
    ),
)


def _literals(node: ast.Call) -> str:
    """Every constant string in this call, positional or keyword, joined."""
    parts: list[str] = []
    for arg in list(node.args) + [kw.value for kw in node.keywords]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts.append(sub.value)
    return " ".join(parts)


def _callee(node: ast.Call) -> str:
    """The called name, whether bare (`make_error`) or attribute (`x.make_error`)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def collect_messages() -> list[tuple[str, int, str]]:
    """Every user-facing literal this guard can reach, with its location."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        if "core" in path.relative_to(SRC).parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _callee(node) not in BUILDERS | RAISERS:
                continue
            text = _literals(node)
            if text:
                found.append((str(path.relative_to(SRC)), node.lineno, text))
    return found


def offending_names(text: str) -> list[str]:
    """Forbidden names this message contains, ignoring allowed subject-uses."""
    if any(marker in text for marker, _reason in ALLOWED):
        return []
    hits = []
    for name in FORBIDDEN:
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            hits.append(name)
    return sorted(hits)


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


def test_no_user_facing_diagnostic_uses_an_internal_name() -> None:
    offenders = [
        f"{filename}:{lineno} uses {names}: {text!r}"
        for filename, lineno, text in collect_messages()
        if (names := offending_names(text))
    ]
    assert not offenders, (
        "a diagnostic uses an internal tool or upstream program name; use the CLI "
        "verb instead, or add an ALLOWED row with its reason (RFC-01, 'Refusals "
        "and exit codes'):\n  " + "\n  ".join(offenders)
    )


def test_the_vendored_not_found_remedy_is_adapted_at_the_call_site() -> None:
    """``core/registry.py`` is verbatim, so its bad remedy is fixed where it surfaces."""
    from browser_tools.core.registry import InstanceNotFoundError
    from browser_tools.lifecycle import instance_not_found_message

    raw = str(InstanceNotFoundError(name="nope", available=[]))
    assert "chrome-agent launch" in raw, "upstream text changed; revisit the adapter"

    adapted = instance_not_found_message(InstanceNotFoundError(name="nope", available=[]))
    assert "chrome-agent" not in adapted
    assert "bt launch" in adapted


# --------------------------------------------------------------------------
# Tests of the guard itself. Without these the guard can silently stop matching.
# --------------------------------------------------------------------------


def test_the_checker_flags_known_bad_messages() -> None:
    """Every wording that shipped, plus the phrasings a remedy-regex would miss."""
    must_flag = [
        "No frame selected. Use select_frame first.",
        "Use list_frames to see available frames.",
        "screencast already recording; call screencast_stop first",
        "no screencast in progress; call screencast_start first",
        "requires a 'uid' from a prior take_snapshot",
        "Launch one with: chrome-agent launch",
        # Phrasings with no remedy verb at all, which an earlier version missed.
        "select_frame first",
        "See select_frame",
        "Invoke select_frame",
        "Recover with select_frame",
        "Run **select_frame**",
    ]
    missed = [m for m in must_flag if not offending_names(m)]
    assert not missed, f"the checker no longer flags: {missed}"


def test_the_checker_passes_known_good_messages() -> None:
    """Real CLI verbs, and forbidden names only as substrings of other words."""
    must_pass = [
        "No frame selected. Pass --key PATTERN to name the frame on this read.",
        "Run 'frames list' to see available frames.",
        "E008: 'click' requires --uid UID from a prior 'snapshot'",
        "Raise it with --max-frames N, or shorten the capture with --duration SECONDS.",
        "Launch one with: bt launch",
        "ax_find requires at least one of 'role' or 'name'",  # the ALLOWED row
        "the select_frames_helper is internal",  # substring, not the name
    ]
    wrongly_flagged = [(m, offending_names(m)) for m in must_pass if offending_names(m)]
    assert not wrongly_flagged, f"the checker flags good messages: {wrongly_flagged}"


def test_collection_reaches_nested_modules_and_exception_messages() -> None:
    """A collector that found nothing would pass the guard for the wrong reason."""
    messages = collect_messages()
    assert len(messages) > 80, f"only {len(messages)} messages; the AST walk regressed"

    joined = {text for _f, _l, text in messages}
    assert any("No frame selected" in t for t in joined), "builder calls not collected"
    assert any("does not look like Domain.method" in t for t in joined), (
        "exception constructions not collected"
    )
