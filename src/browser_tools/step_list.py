"""Read a Step List, and refuse a bad one before any step runs.

A **Step List** is one step per line, in the grammar The Manual already
documents. RFC-03 specifies it as data, never code: no branch, no loop, no
variable, and no way for one step to read another step's result. Parsing it is
therefore splitting lines into argv and checking each one, and nothing more.

The one rule that shapes this module: **a Step Run validates every step before
it executes the first one** (RFC-03, "Validation, before any step runs"). A
malformed step anywhere means exit 2, nothing on stdout, and nothing executed.
Discovering a typo at step 8 after clicking through steps 1 to 7 would break
the exit-2 contract The Manual states, and the caller would have no way to know
it had been broken.

Nothing here reaches the browser. A UID that no longer resolves, a frame
pattern that matches nothing, a CDP method Chrome does not have: those are
runtime failures found at their own step, not usage errors.

Why it reuses the CLI's own parser
----------------------------------
A second parser that matches the first is a second parser that will stop
matching it. One disagreement about one flag turns a usage error into a runtime
failure or the reverse, and the difference would show up as a step behaving
differently inside a run than outside it. This repository already carries a
live example of two lists disagreeing about one verb, which is why
``STEP_VERBS`` below is written out rather than derived by subtraction.

So a step is parsed by handing its argv to ``cli.build_parser``, and its
per-verb requirements are checked by ``cli.check_preconditions``, the same
function ``cli._run`` calls. Neither is copied.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import lifecycle, passthrough
from .usage import UsageError

if TYPE_CHECKING:
    import argparse

#: Verbs that drive the attached browser, and so may be a step. Written out
#: rather than derived: RFC-01 and ``GUIDE.txt`` disagree about whether
#: ``window-border`` takes ``--endpoint``, and a set built by subtracting one
#: of them would inherit the disagreement.
STEP_VERBS = frozenset(
    {
        "snapshot",
        "click",
        "fill",
        "wait-idle",
        "wait-stable",
        "wait",
        "detect",
        "console-list",
        "network-list",
        "frames",
        "storage",
        "screenshot",
        "screencast",
    }
)

#: Verbs that exist but cannot be a step, each with the reason a caller needs.
EXCLUDED_VERBS: dict[str, str] = {
    "attach": (
        "it streams until stdin reaches EOF, so it has no bounded end and could "
        "never hand control to the next step"
    ),
    "launch": (
        "it does not drive the attached browser, and it would change the "
        "instance the run has already resolved and connected to"
    ),
    "stop": (
        "it does not drive the attached browser, and it would change the "
        "instance the run has already resolved and connected to"
    ),
    "cleanup": "it does not drive the attached browser",
    "status": "it does not drive the attached browser",
    "profile": "it does not drive the attached browser",
    "window-border": "it does not drive the attached browser",
    "guide": "it does not drive the attached browser",
    "help": "it does not drive the attached browser",
}

#: Flags the invocation owns. A step carrying one is a usage error: the
#: connection is opened once, for the whole run, before step 1.
INVOCATION_FLAGS = ("--endpoint", "--target", "--url")


@dataclass(frozen=True)
class Step:
    """One validated step, ready to execute.

    ``number`` counts steps, so it is what a diagnostic and the Run Document
    name. ``line`` counts lines in the source, including the comments and
    blanks that are not steps, so it is what a caller needs to find the text.
    They are different numbers and both are kept.
    """

    number: int
    line: int
    text: str
    argv: list[str]
    #: The parsed namespace for a curated verb, None for a passthrough step.
    args: argparse.Namespace | None
    #: ``Domain.method`` for a passthrough step, None for a curated verb.
    method: str | None = None
    #: The passthrough step's params, already parsed from JSON.
    params: dict[str, Any] | None = None

    @property
    def is_passthrough(self) -> bool:
        return self.method is not None


class StepListError(UsageError):
    """A Step List that cannot run (exit 2). Nothing was sent or written."""


def _at(line: int, message: str) -> StepListError:
    return StepListError(f"line {line}: {message}")


def split_lines(text: str) -> list[tuple[int, str, list[str]]]:
    """Split a Step List into ``(line number, text, argv)`` per step.

    Comments and blank lines are dropped here, so what comes back is steps
    only. Line numbers survive the drop, because a diagnostic that counts
    steps cannot point a caller at the text.

    ``shlex`` in POSIX mode gives the quoting rules a caller already uses at
    the shell, so ``Page.navigate '{"url": "..."}'`` reads the same in a Step
    List as it does on the command line.
    """
    steps: list[tuple[int, str, list[str]]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            argv = shlex.split(stripped, posix=True)
        except ValueError as exc:
            raise _at(line_number, f"cannot parse this line: {exc}") from exc
        if not argv:
            continue
        steps.append((line_number, stripped, argv))
    return steps


def _parse_with_cli_parser(argv: list[str], line: int) -> argparse.Namespace:
    """Parse one step's argv with the CLI's own parser.

    ``argparse`` reports a failure by writing to stderr and raising
    ``SystemExit``, which inside a Step Run would end the run rather than
    report a step. Both are intercepted, so the diagnostic names the step's
    line instead of appearing as a bare parser message about a program the
    caller did not invoke.
    """
    from .cli import build_parser

    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            return build_parser().parse_args(argv)
    except SystemExit as exc:
        detail = captured.getvalue().strip().splitlines()
        message = detail[-1] if detail else f"argparse rejected this step (exit {exc.code})"
        raise _at(line, message) from exc


def _check_not_an_instance(argv: list[str], line: int, known: set[str]) -> None:
    """A step names no instance. The run resolved one before step 1."""
    if argv[0] in known:
        raise _at(
            line,
            f"a step must not name an instance ('{argv[0]}'): "
            "the run resolves the instance once, before step 1",
        )


def _refuse_invocation_flag(flag: str, line: int) -> StepListError:
    return _at(
        line,
        f"a step must not carry {flag}: "
        "the run opens one connection to one page, before step 1",
    )


def _check_no_invocation_flags(argv: list[str], line: int) -> None:
    """Reject the flags the invocation owns, before the step is parsed.

    Scanning tokens catches only what the caller typed in full. It is here
    for the passthrough step, which never reaches ``argparse``, and for a
    message that names the flag as written. The authoritative check for a
    curated verb is ``_check_nothing_was_retargeted`` below, on the parsed
    result.
    """
    for token in argv:
        flag = token.split("=", 1)[0]
        if flag in INVOCATION_FLAGS:
            raise _refuse_invocation_flag(flag, line)


def _check_nothing_was_retargeted(args: argparse.Namespace, line: int) -> None:
    """Reject an invocation flag by what ``argparse`` produced, not by spelling.

    ``argparse`` accepts any unambiguous prefix of a long option, so
    ``--targ 1`` sets ``target`` and ``--end URL`` sets ``endpoint`` while a
    scan for the full flag name sees neither. Left to the scan alone, a step
    could silently drive a different page, or a different browser.

    Reading the namespace is immune to the spelling: whatever the caller
    wrote, this is what the step would have used.
    """
    for attribute, flag in (("target", "--target"), ("url", "--url"), ("endpoint", "--endpoint")):
        if getattr(args, attribute, None) is not None:
            raise _refuse_invocation_flag(flag, line)


def _validate_passthrough(argv: list[str], line: int) -> tuple[str, dict[str, Any] | None]:
    """Check a ``Domain.method '{...}'`` step and return its method and params.

    The focus refusals are checked here rather than at the step, because they
    can be checked here: the method name and the params are both on the line.
    RFC-03 puts them in the exit-2 class for that reason, so a Step List
    containing one runs nothing at all.
    """
    if len(argv) > 2:
        raise _at(line, f"'{argv[0]}' takes at most one JSON params argument")

    method = argv[0]
    params: dict[str, Any] | None = None
    if len(argv) == 2:
        try:
            decoded = json.loads(argv[1])
        except json.JSONDecodeError as exc:
            raise _at(line, f"params are not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise _at(line, "params must be a JSON object")
        params = decoded

    try:
        passthrough.guard_focus(method, params)
    except UsageError as exc:
        raise _at(line, str(exc)) from exc

    return method, params


def validate(text: str, registry_path: str | None = None) -> list[Step]:
    """Parse a Step List and return its steps, or raise ``StepListError``.

    The registry is read once, here, and the whole run uses the result. It is
    mutable and another process can change it mid-run, so a run that re-read it
    per step could classify the same token two ways in one invocation.
    """
    from .cli import check_preconditions

    raw_steps = split_lines(text)
    if not raw_steps:
        raise StepListError(
            "the Step List is empty: it has no steps after comments and blank lines"
        )

    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    steps: list[Step] = []

    for number, (line, step_text, argv) in enumerate(raw_steps, start=1):
        _check_not_an_instance(argv, line, known)
        _check_no_invocation_flags(argv, line)

        head = argv[0]

        if head in EXCLUDED_VERBS:
            raise _at(line, f"'{head}' cannot be a step: {EXCLUDED_VERBS[head]}")

        if head in STEP_VERBS:
            args = _parse_with_cli_parser(argv, line)
            _check_nothing_was_retargeted(args, line)
            if getattr(args, "instance", None) is not None:
                raise _at(
                    line,
                    f"a step must not name an instance ('{args.instance}'): "
                    "the run resolves the instance once, before step 1",
                )
            try:
                check_preconditions(args)
            except UsageError as exc:
                raise _at(line, str(exc)) from exc
            steps.append(Step(number=number, line=line, text=step_text, argv=argv, args=args))
            continue

        if lifecycle.looks_like_domain_method(head):
            method, params = _validate_passthrough(argv, line)
            steps.append(
                Step(
                    number=number,
                    line=line,
                    text=step_text,
                    argv=argv,
                    args=None,
                    method=method,
                    params=params,
                )
            )
            continue

        raise _at(
            line,
            f"'{head}' is not a step. A step is one of "
            f"{', '.join(sorted(STEP_VERBS))}, or Domain.method with optional JSON params",
        )

    return steps


__all__ = [
    "EXCLUDED_VERBS",
    "INVOCATION_FLAGS",
    "STEP_VERBS",
    "Step",
    "StepListError",
    "split_lines",
    "validate",
]
