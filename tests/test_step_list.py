"""A Step List is refused whole, before any step runs.

RFC-03's validation rule is the one thing these tests exist to hold: a
malformed step anywhere means exit 2 and nothing executed. The exit-2 contract
The Manual states is "nothing was sent, written or deleted", and a run that
found the typo at step 8 would have broken it seven steps ago.

The second rule under test is that validation reuses the CLI's own parser and
the CLI's own precondition checks, rather than matching them. A copy drifts,
and the drift shows up as a step behaving differently inside a run than
outside it.
"""

from __future__ import annotations

import argparse

import pytest

from browser_tools import cli, step_list
from browser_tools.step_list import StepListError, validate


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


@pytest.fixture
def no_instances(monkeypatch):
    """No registered instance, so no bare token resolves as one."""
    monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda **_: [])


@pytest.fixture
def one_instance(monkeypatch):
    """One registered instance named `only-01`."""

    class Instance:
        name = "only-01"

    monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda **_: [Instance()])


class TestTheLineGrammar:
    """One step per line, in the grammar The Manual already documents."""

    def test_each_non_empty_line_is_a_step(self, no_instances):
        steps = validate("snapshot\nwait-idle\ndetect\n")
        assert [s.argv for s in steps] == [["snapshot"], ["wait-idle"], ["detect"]]

    def test_steps_keep_their_order(self, no_instances):
        steps = validate("detect\nsnapshot\n")
        assert [s.number for s in steps] == [1, 2]
        assert steps[0].argv == ["detect"]

    def test_a_comment_line_is_ignored(self, no_instances):
        steps = validate("# select the checkout frame\nsnapshot\n")
        assert len(steps) == 1

    def test_an_indented_comment_is_ignored(self, no_instances):
        steps = validate("    # still a comment\nsnapshot\n")
        assert len(steps) == 1

    def test_blank_and_whitespace_lines_are_ignored(self, no_instances):
        steps = validate("\nsnapshot\n   \n\t\ndetect\n")
        assert len(steps) == 2

    def test_only_a_leading_hash_starts_a_comment(self, no_instances):
        """A `#` inside a token is literal, as it is at the shell."""
        steps = validate("frames select checkout#1\n")
        assert steps[0].argv == ["frames", "select", "checkout#1"]

    def test_a_trailing_comment_is_not_a_comment(self, no_instances):
        """The RFC says first non-whitespace character, so this is argv.

        The step then fails to parse, which is the right answer: silently
        dropping a trailing `#` would make `fill --text '#1'` ambiguous.
        """
        with pytest.raises(StepListError):
            validate("snapshot  # read the page\n")

    def test_quoting_reads_as_it_does_at_the_shell(self, no_instances):
        steps = validate("""Page.navigate '{"url": "https://example.com"}'\n""")
        assert steps[0].method == "Page.navigate"
        assert steps[0].params == {"url": "https://example.com"}

    def test_an_unbalanced_quote_is_a_usage_error(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("snapshot\nfill --uid 1-2 --text 'unclosed\n")
        assert "line 2" in str(caught.value)

    def test_the_line_number_survives_comments_and_blanks(self, no_instances):
        """The step is the third step but the sixth line."""
        text = "# a\n\nsnapshot\n# b\n\ndetect\n"
        steps = validate(text)
        assert (steps[1].number, steps[1].line) == (2, 6)


class TestAnEmptyStepListIsRefused:
    def test_no_lines_at_all(self, no_instances):
        with pytest.raises(StepListError):
            validate("")

    def test_only_comments_and_blanks(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("# nothing here\n\n   \n")
        assert "empty" in str(caught.value)


class TestTheStepSurface:
    @pytest.mark.parametrize("verb", sorted(step_list.STEP_VERBS - {"click", "fill"}))
    def test_every_listed_verb_is_accepted_bare(self, verb, no_instances):
        """Each of these needs nothing beyond the verb to be a valid step."""
        argv = {
            "frames": "frames list",
            "storage": "storage get",
            "screencast": "screencast --dir /tmp/x",
            "wait": "wait --event Page.loadEventFired",
        }.get(verb, verb)
        assert len(validate(argv + "\n")) == 1

    @pytest.mark.parametrize("verb", sorted(step_list.EXCLUDED_VERBS))
    def test_every_excluded_verb_is_refused_with_a_reason(self, verb, no_instances):
        with pytest.raises(StepListError) as caught:
            validate(f"{verb}\n")
        message = str(caught.value)
        assert verb in message
        assert step_list.EXCLUDED_VERBS[verb] in message

    def test_attach_is_refused_for_its_own_reason(self, no_instances):
        """It streams to EOF, so it could never hand control to step 2."""
        with pytest.raises(StepListError) as caught:
            validate("attach Page.loadEventFired\n")
        assert "bounded end" in str(caught.value)

    def test_an_unknown_verb_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("frobnicate\n")
        assert "not a step" in str(caught.value)

    def test_a_flag_in_the_verb_position_is_refused(self, no_instances):
        """`--version` would otherwise exit 0 out of the middle of a run."""
        with pytest.raises(StepListError):
            validate("--version\n")


class TestTheSurfaceCoversEveryVerbTheParserKnows:
    """The guard against a new verb being silently neither.

    `test_guide.py` fails the build when a verb has no manual entry. This
    fails it when a verb is neither a step nor explicitly excluded, so adding
    one forces the decision instead of defaulting it.
    """

    def test_no_verb_is_unaccounted_for(self):
        parser = cli.build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        assert subparsers, "the parser grew no subcommands"
        verbs = set(subparsers[0].choices)

        accounted = step_list.STEP_VERBS | set(step_list.EXCLUDED_VERBS)
        unaccounted = verbs - accounted
        assert not unaccounted, (
            f"{sorted(unaccounted)} is neither a step nor excluded. Add it to "
            "STEP_VERBS if it drives the attached browser, or to EXCLUDED_VERBS "
            "with the reason it cannot."
        )

    def test_nothing_is_claimed_that_the_parser_does_not_have(self):
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        verbs = set(subparsers.choices)
        claimed = step_list.STEP_VERBS | set(step_list.EXCLUDED_VERBS)
        assert not (claimed - verbs), f"{sorted(claimed - verbs)} is not a verb"


class TestTheInvocationOwnsSomeFlags:
    @pytest.mark.parametrize("flag", ["--endpoint http://127.0.0.1:9222", "--target 1", "--url ex"])
    def test_a_step_may_not_carry_one(self, flag, no_instances):
        with pytest.raises(StepListError) as caught:
            validate(f"snapshot {flag}\n")
        assert "must not carry" in str(caught.value)

    def test_the_equals_form_is_caught_too(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("snapshot --target=1\n")
        assert "--target" in str(caught.value)

    @pytest.mark.parametrize(
        ("step", "flag"),
        [
            ("snapshot --targ 1", "--target"),
            ("snapshot --t 1", "--target"),
            ("snapshot --end http://127.0.0.1:9222", "--endpoint"),
            ("console-list --u example", "--url"),
        ],
    )
    def test_an_abbreviated_flag_is_caught_too(self, step, flag, no_instances):
        """`argparse` accepts any unambiguous prefix of a long option.

        A scan of the raw tokens sees `--targ` and finds nothing to refuse,
        while `argparse` sets `target` from it. The step would then drive a
        different page, or with `--end`, a different browser.
        """
        with pytest.raises(StepListError) as caught:
            validate(step + "\n")
        assert flag in str(caught.value)

    def test_an_abbreviated_flag_on_a_passthrough_step_is_caught(self, no_instances):
        """No argparse here, so the arity rule is what catches it."""
        with pytest.raises(StepListError):
            validate("Page.navigate '{}' --targ 1\n")

    def test_a_step_may_not_name_an_instance(self, one_instance):
        with pytest.raises(StepListError) as caught:
            validate("only-01 snapshot\n")
        assert "must not name an instance" in str(caught.value)

    def test_the_instance_as_a_positional_is_caught(self, one_instance):
        """`snapshot only-01` parses: `instance` is a positional on every verb."""
        with pytest.raises(StepListError) as caught:
            validate("snapshot only-01\n")
        assert "must not name an instance" in str(caught.value)

    def test_a_token_the_registry_does_not_know_is_not_an_instance(self, no_instances):
        """With no registry entry, the same line is a Domain.method question."""
        with pytest.raises(StepListError) as caught:
            validate("only-01 snapshot\n")
        assert "not a step" in str(caught.value)


class TestThePreconditionsAreTheCLIsOwn:
    """RFC-03: reused, never copied, because a copy drifts."""

    def test_click_without_a_uid_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("click\n")
        assert "requires --uid" in str(caught.value)

    def test_fill_without_a_text_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("fill --uid AB-1\n")
        assert "requires --text" in str(caught.value)

    def test_storage_without_its_sub_action_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("storage\n")
        assert "one sub-action" in str(caught.value)

    def test_screencast_without_a_dir_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("screencast\n")
        assert "requires --dir" in str(caught.value)

    def test_screencast_with_a_removed_sub_action_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("screencast start --dir /tmp/x\n")
        assert "no sub-action" in str(caught.value)

    def test_the_message_is_the_cli_s_own(self, no_instances):
        """Not a lookalike written here. Same words, from the same function."""
        expected = None
        try:
            cli.check_preconditions(argparse.Namespace(command="click", uid=None))
        except Exception as exc:
            expected = str(exc)
        with pytest.raises(StepListError) as caught:
            validate("click\n")
        assert expected and expected in str(caught.value)


class TestTheFocusRefusalsAreCaughtUpFront:
    """They are checkable without the browser, so RFC-03 puts them at exit 2."""

    @pytest.mark.parametrize("method", ["Target.activateTarget", "Page.bringToFront"])
    def test_a_focus_taking_method_refuses_the_whole_list(self, method, no_instances):
        with pytest.raises(StepListError) as caught:
            validate(f"snapshot\n{method}\nsnapshot\n")
        assert "refused" in str(caught.value)
        assert "line 2" in str(caught.value)

    def test_create_target_in_the_foreground_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("""Target.createTarget '{"background": false}'\n""")
        assert "refused" in str(caught.value)

    def test_create_target_in_the_background_is_allowed(self, no_instances):
        assert len(validate("""Target.createTarget '{"url": "about:blank"}'\n""")) == 1


class TestPassthroughSteps:
    def test_a_method_with_no_params_is_a_step(self, no_instances):
        steps = validate("Page.getNavigationHistory\n")
        assert steps[0].method == "Page.getNavigationHistory"
        assert steps[0].params is None
        assert steps[0].is_passthrough

    def test_malformed_json_is_a_usage_error(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("Page.navigate '{url: broken}'\n")
        assert "not valid JSON" in str(caught.value)

    def test_json_that_is_not_an_object_is_a_usage_error(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("Page.navigate '[1, 2]'\n")
        assert "JSON object" in str(caught.value)

    def test_a_third_argument_is_a_usage_error(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("Page.navigate '{}' extra\n")
        assert "at most one" in str(caught.value)

    def test_a_curated_step_is_not_marked_passthrough(self, no_instances):
        assert validate("snapshot\n")[0].is_passthrough is False


class TestValidationIsWholeListBeforeAnyStep:
    """The property the whole module exists for."""

    def test_a_late_bad_step_refuses_the_whole_list(self, no_instances):
        text = "snapshot\ndetect\nwait-idle\nclick\nsnapshot\n"
        with pytest.raises(StepListError) as caught:
            validate(text)
        assert "line 4" in str(caught.value)

    def test_the_first_failure_is_the_one_reported(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("click\nfill\n")
        assert "line 1" in str(caught.value)

    def test_a_good_list_returns_every_step(self, no_instances):
        text = (
            "# read the checkout frame twice\n"
            "frames select checkout\n"
            "storage get\n"
            "Page.getNavigationHistory '{}'\n"
            "snapshot\n"
        )
        steps = validate(text)
        assert [s.number for s in steps] == [1, 2, 3, 4]
        assert [s.line for s in steps] == [2, 3, 4, 5]
        assert steps[2].is_passthrough
