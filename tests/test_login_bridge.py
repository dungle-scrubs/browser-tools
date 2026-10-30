"""The bridge contract between the Python caller and the injected helper.

What ``login.py`` may rely on from ``login_form.js``: the helper's exported
entry points stay exactly the entry points the real Python caller evaluates
(derived from login.py's own AST, counting only the string literals of the
evaluated expression argument of ``_eval_login_script``), so a dead export
- or an inert mention smuggled through a diagnostic argument, a comment or
a docstring - fails here, named, with the instruction to remove it.

Extracted from ``test_login_live.py`` (repository module-size rule,
round-six Q10): the disposable Chrome and the loopback HTTPS site fixtures
are re-exported from login_fixtures.py explicitly; everything here is
synthetic.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

from browser_tools import lifecycle
from browser_tools.one_shot import one_shot_page_session

# The pytest fixtures are re-exported under their own names (the explicit
# `x as x` alias is pyflakes' re-export form), so pytest resolves them from
# this module while lint stays clean without any waiver. `site` is not
# requested by a test here, but it is a dependency of `disposable` and must
# be registered in this module for that fixture to resolve.
from login_fixtures import disposable as disposable
from login_fixtures import site as site


def _expression_parameter() -> tuple[str, int]:
    """The evaluated-expression parameter of ``_eval_login_script``.

    Located through the function's real signature - its name and its
    positional index - so a rename or a reorder fails this guard loudly
    instead of silently degrading into scanning every argument (the
    round-four B6-R4 mistake this guard exists to reject).
    """
    import inspect

    from browser_tools import login as login_module

    name = "expression_tail"
    signature = inspect.signature(login_module._eval_login_script)
    parameter = signature.parameters.get(name)
    positional = [
        item.name
        for item in signature.parameters.values()
        if item.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if parameter is None or name not in positional:
        raise AssertionError(
            "_eval_login_script no longer takes its evaluated expression as "
            f"the parameter {name!r}; update this guard to the real signature "
            "so only the evaluated argument is ever scanned"
        )
    return name, positional.index(name)


def _evaluated_argument(call: ast.Call, name: str, position: int) -> ast.AST | None:
    """The one argument bound to the expression parameter, or None.

    Keyword first, else the positional slot at the parameter's index. A
    call that unpacks star arguments cannot be resolved and fails loudly
    rather than guess - expanding it keeps the guard exact.
    """
    if any(isinstance(argument, ast.Starred) for argument in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        raise AssertionError(
            "a call to _eval_login_script unpacks star arguments; expand "
            "them so the evaluated expression can be identified exactly"
        )
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    if len(call.args) > position:
        return call.args[position]
    return None


def _helper_entry_calls(source: str) -> set[str]:
    """Entry points the Python caller actually calls, derived from the AST.

    Only the evaluated expression argument of ``_eval_login_script``
    counts - located through that function's real signature, by positional
    index or keyword name - because entry points are called as
    ``__btLogin.<name>(`` inside string literals (plain or the literal
    parts of f-strings) inside the evaluated source. Every other argument
    (``phase`` diagnostics, ``return_by_value``, ids, budgets) never
    reaches the browser, so a mention seeded there cannot keep a dead
    export alive; comments never parse, and docstrings are not arguments
    (round-three B6, round-four B6-R4).
    """

    def literal_strings(node: object) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return [
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
        return []

    parameter, position = _expression_parameter()
    uses: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_eval_login_script"
        ):
            continue
        argument = _evaluated_argument(node, parameter, position)
        if argument is None:
            continue
        for text in literal_strings(argument):
            uses.update(
                name for name in re.findall(r"__btLogin\.(\w+)\s*\(", text)
            )
    return uses


class TestBridgeContract:
    def test_an_inert_phase_diagnostic_is_not_an_evaluated_use(self):
        """Round-four B6-R4: only the evaluated expression argument counts.

        The exact round-four witness: a diagnostic string naming a dead
        entry point, seeded into the ``phase`` keyword - an argument that
        never reaches the browser - must not keep that entry point alive.
        """
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        before = 'return_by_value=False, phase="form validation", deadline=deadline'
        after = (
            'return_by_value=False, phase="diagnostic __btLogin.probe(", '
            "deadline=deadline"
        )
        assert caller.count(before) == 1, "the caller's validation call moved; reseed"
        seeded = caller.replace(before, after, 1)
        assert _helper_entry_calls(seeded) == _helper_entry_calls(caller) == {"begin"}, (
            "an argument other than the evaluated expression legitimized "
            "an entry point"
        )

    def test_inert_mentions_in_other_arguments_are_not_uses(self):
        """No argument of the evaluation call reaches the browser except
        the evaluated expression, so a dead-export mention seeded into
        ``return_by_value`` counts for nothing either."""
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        before = 'return_by_value=False, phase="form validation"'
        after = 'return_by_value="__btLogin.probe(", phase="form validation"'
        assert caller.count(before) == 1, "the caller's validation call moved; reseed"
        seeded = caller.replace(before, after, 1)
        assert _helper_entry_calls(seeded) == {"begin"}, (
            "an argument other than the evaluated expression legitimized "
            "an entry point"
        )

    def test_the_expression_argument_by_keyword_still_counts(self):
        """The guard follows the real signature, so moving the evaluated
        expression to a keyword argument cannot hide a genuine use."""
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        before = 'f"__btLogin.begin({policy_json})",'
        after = 'expression_tail=f"__btLogin.begin({policy_json})",'
        assert caller.count(before) == 1, "the caller's begin call moved; reseed"
        seeded = caller.replace(before, after, 1)
        assert _helper_entry_calls(seeded) == {"begin"}, (
            "the evaluated expression, passed by keyword, was not counted"
        )

    def test_a_seeded_comment_or_docstring_mention_is_not_a_use(self):
        """The repeat guard reads actual call strings, never prose.

        Seeding both a comment and a docstring mention of a dead entry
        point into the caller's source must not count it as a use, nor
        may a diagnostic string; only the evaluated ``begin`` call does.
        """
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        assert _helper_entry_calls(caller) == {"begin"}, (
            "the caller's real entry-point calls changed; update this proof "
            "and the rule table together"
        )
        seeded = caller.replace(
            '"""',
            '"""Seeded mention: __btLogin.probe(policy) is not a use.',
            1,
        ).replace(
            "def _policy_json_for_page(",
            "# __btLogin.probe(\ndef _policy_json_for_page(",
            1,
        )
        seeded += '\nlogger.debug("__btLogin.probe(")\n'
        assert _helper_entry_calls(seeded) == {"begin"}, (
            "a seeded prose or diagnostic mention passed as a helper use"
        )

    def test_helper_exports_match_the_python_caller(self, disposable):
        """One enforcing check for the repeat dead-export mistake.

        The helper's exported entry points must be exactly the entry points
        the Python caller still uses (derived from login.py's own AST, so
        only actual ``__btLogin.<name>(`` call strings count), so an export
        left behind by a rewrite - like the obsolete success probe - fails
        here, named, with the instruction to remove it.
        """
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        used = sorted(_helper_entry_calls(caller))
        assert "begin" in used, "the Python caller must use the form entry point"

        async def exports_of_helper() -> list[str]:
            port = lifecycle.resolve_cdp_port(
                disposable.name, registry_path=disposable.registry
            )
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                tree = await cdp.send(
                    "Page.getFrameTree", {}, session_id=session_id, timeout=5.0
                )
                frame_id = tree["frameTree"]["frame"]["id"]
                world = await cdp.send(
                    "Page.createIsolatedWorld",
                    {"frameId": frame_id, "worldName": "bridge-contract",
                     "grantUniveralAccess": False},
                    session_id=session_id, timeout=5.0,
                )
                reply = await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            login_module._login_script_source()
                            + "\n;Object.keys(globalThis.__btLogin).sort()"
                        ),
                        "contextId": world["executionContextId"],
                        "returnByValue": True,
                    },
                    session_id=session_id, timeout=5.0,
                )
                return reply["result"]["value"]

        exports = asyncio.run(exports_of_helper())
        unused = sorted(set(exports) - set(used))
        assert not unused, (
            f"login_form.js exports {unused}, which the Python caller no longer "
            "uses; remove the dead entry point(s) and their contract comment"
        )
        missing = sorted(set(used) - set(exports))
        assert not missing, (
            f"the Python caller uses {missing}, which the helper no longer exports"
        )
