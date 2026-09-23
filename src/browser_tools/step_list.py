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

Nothing here defines a second parser. A step goes to ``cli.build_parser``
and ``cli.check_preconditions``, the ones ``cli._run`` uses. A copy that
disagreed about one flag would turn a usage error into a runtime failure or
the reverse, and a step would behave differently inside a run than outside
it. ``STEP_VERBS`` is written out for the same reason: two lists in this
repository already disagree about one verb.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import endpoint as endpoint_module
from . import lifecycle, passthrough
from .one_shot import RUN_OWNED_DOMAINS
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
        "eval",
        "press",
        "hover",
        "type",
        "wait-text",
        "network-get",
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
        "heap",
    }
)

#: Verbs that exist but cannot be a step, each with the reason a caller needs.
EXCLUDED_VERBS: dict[str, str] = {
    "navigate": "use a raw Page.navigate step; the run owns its dialog policy",
    "trace": "a trace owns a Step Run; nesting it inside a run is not supported",
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
    "run": (
        "a Step List is data, not code: a run inside a run would be nesting, "
        "and nesting is control flow. Put the steps in one list"
    ),
}

#: Flags the invocation owns. A step carrying one is a usage error: the
#: connection is opened once, for the whole run, before step 1.
INVOCATION_FLAGS = ("--endpoint", "--target", "--url", "--dialog")


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

    def __post_init__(self) -> None:
        """Exactly one of ``args`` and ``method`` is set.

        A step is a curated verb or a raw CDP method, never both and never
        neither. Checked here so the two accessors below can promise what
        they return, instead of every caller re-testing it and one of them
        getting it wrong.
        """
        if (self.args is None) == (self.method is None):
            raise ValueError(
                f"step {self.number} must carry either a parsed verb or a CDP method, "
                f"not {'both' if self.args is not None else 'neither'}"
            )

    @property
    def is_passthrough(self) -> bool:
        return self.method is not None

    def as_verb(self) -> argparse.Namespace:
        """The parsed namespace, for a curated step."""
        if self.args is None:
            raise ValueError(f"step {self.number} is a raw CDP step, not a verb")
        return self.args

    def as_method(self) -> tuple[str, dict[str, Any] | None]:
        """``(method, params)``, for a raw CDP step."""
        if self.method is None:
            raise ValueError(f"step {self.number} is a verb, not a raw CDP step")
        return self.method, self.params


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

    ``argparse`` ends the process to report itself. A bad flag writes to
    stderr and raises ``SystemExit(2)``; ``--help`` writes to **stdout** and
    raises ``SystemExit(0)``. Inside a Step Run either would end the run
    rather than report a step, and the help text would land on the stdout
    that a refused run promises to leave empty.

    So both streams are captured and ``SystemExit`` is converted, whatever
    its code. The diagnostic then names the step's line instead of appearing
    as a bare parser message about a program the caller did not invoke.
    """
    from .cli import build_parser

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return build_parser().parse_args(argv)
    except SystemExit as exc:
        raise _at(line, _why_argparse_gave_up(argv, err.getvalue(), exc.code)) from exc


def _why_argparse_gave_up(argv: list[str], stderr: str, code: object) -> str:
    """Turn one ``SystemExit`` from the parser into a message about the step."""
    if code == 0:
        # --help or --version. Not an error to argparse, not a step either.
        return (
            f"'{argv[0]}' was asked to print help rather than run: "
            "a step is an action, and the run prints nothing but its own output. "
            "Use `bt guide` outside the run."
        )
    detail = stderr.strip().splitlines()
    return detail[-1] if detail else f"the parser rejected this step (exit {code})"


def _check_not_an_instance(argv: list[str], line: int, known: set[str]) -> None:
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
    """Reject the flags the invocation owns, for a step with no parser.

    Passthrough steps only. On a curated step this would misread a
    positional and refuse the valid ``frames select -- --target``, whose
    pattern is the word ``--target``.
    """
    for token in argv:
        flag = token.split("=", 1)[0]
        if flag in INVOCATION_FLAGS:
            raise _refuse_invocation_flag(flag, line)


def _check_nothing_was_retargeted(args: argparse.Namespace, line: int) -> None:
    """Reject an invocation flag by what ``argparse`` produced, not by spelling.

    ``argparse`` accepts any unambiguous prefix of a long option, so
    ``--targ 1`` sets ``target`` and ``--end URL`` sets ``endpoint``. A scan
    for the full name sees neither, and the step drives a different page, or
    a different browser.
    """
    from .cli import url_selects_page

    if getattr(args, "reload", False):
        raise _at(line, "network-get --reload is standalone only; a run preserves earlier steps")
    for attribute, flag in (("target", "--target"), ("url", "--url"), ("endpoint", "--endpoint"), ("dialog", "--dialog")):
        if attribute == "url" and not url_selects_page(args.command):
            continue
        if getattr(args, attribute, None) is not None:
            raise _refuse_invocation_flag(flag, line)


def _validate_passthrough(
    argv: list[str], line: int, endpoint: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Check a ``Domain.method '{...}'`` step and return its method and params.

    The focus refusals are checked here rather than at the step, because they
    can be checked here: the method name and the params are both on the line.
    RFC-03 puts them in the exit-2 class for that reason, so a Step List
    containing one runs nothing at all.

    ``endpoint`` is the run's, not the step's. RFC-03 reasoned that the
    ``Browser.close`` and ``Browser.crash`` refusals were unreachable inside a
    run because a step cannot carry ``--endpoint``. That was wrong: the run
    carries it, and every step is attached to that external browser. Without
    this, ``bt run steps --endpoint URL`` with ``Browser.close`` on a line
    closed a browser the tool does not own, where the bare invocation refuses.
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

    if endpoint is not None:
        try:
            endpoint_module.refuse_browser_lifetime_method(method)
        except UsageError as exc:
            raise _at(line, str(exc)) from exc

    domain, _, call = method.partition(".")
    if call == "disable" and domain in RUN_OWNED_DOMAINS:
        raise _at(
            line,
            f"{method} cannot be a step: a run keeps {domain} enabled throughout "
            "to track frames and responses. Turning it off invalidates that state. "
            "Measured: with Page disabled, a "
            "navigation went unseen, 'frames list' reported the page the run "
            f"had left, and 'storage get' read a frame that no longer existed "
            f"and exited 0. Run '{method}' on its own, outside a run.",
        )

    if method == "Target.setAutoAttach":
        raise _at(
            line,
            "Target.setAutoAttach cannot be a step: a run holds one CDP "
            "session per cross-origin iframe, and turning auto-attach off "
            "drops every one of them for the rest of the run. 'frames list' "
            "would stop showing those frames and a selection into one would "
            "fail, with nothing reporting why. It is the shape of "
            "'Page.disable' above and it is refused for the same reason. Use "
            "'--frames page', which is the default, to run without them.",
        )

    try:
        # The return value is the point, not just the refusal: for
        # `Target.createTarget` it adds `background: true`, which is what keeps
        # a new tab from raising the browser over the user's work. The bare
        # path keeps it (`passthrough.py:310`), so the Step must too.
        params = passthrough.guard_focus(method, params)
    except UsageError as exc:
        raise _at(line, str(exc)) from exc

    return method, params


def validate(
    text: str, registry_path: str | None = None, endpoint: str | None = None
) -> list[Step]:
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
                check_preconditions(args, known_instances=known)
            except UsageError as exc:
                raise _at(line, str(exc)) from exc
            if getattr(args, "instance", None) is not None:
                raise _at(line, "a step must not name an instance; the run resolves it once")
            steps.append(Step(number=number, line=line, text=step_text, argv=argv, args=args))
            continue

        if lifecycle.looks_like_domain_method(head):
            _check_no_invocation_flags(argv, line)
            method, params = _validate_passthrough(argv, line, endpoint)
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
