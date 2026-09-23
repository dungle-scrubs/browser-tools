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
import contextlib

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
            "heap": "heap --out /tmp/x.heapsnapshot",
            "wait": "wait --event Page.loadEventFired",
            "eval": "eval '1+1'",
            "press": "press Enter",
            "hover": "hover --uid AB-1",
            "type": "type hello",
            "wait-text": "wait-text ready",
            "network-get": "network-get --request-id 1.2",
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

    @pytest.mark.parametrize("step", ["snapshot --help", "snapshot -h"])
    def test_a_help_step_is_refused_and_prints_nothing(self, step, no_instances, capsys):
        """`--help` is the one argparse case that exits 0 and writes stdout.

        A refused run promises an empty stdout. Letting 417 characters of
        help text through would break that while still exiting 2, which is
        the worst of both.
        """
        with pytest.raises(StepListError) as caught:
            validate(step + "\n")
        assert "print help" in str(caught.value)
        assert capsys.readouterr().out == "", "the parser's help reached stdout"

    def test_a_bad_flag_still_reports_the_parser_s_own_words(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("snapshot --bogus\n")
        assert "--bogus" in str(caught.value)

    def test_a_bad_flag_prints_nothing_either(self, no_instances, capsys):
        with pytest.raises(StepListError):
            validate("snapshot --bogus\n")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "", "the parser's usage message reached stderr"

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
    @pytest.mark.parametrize(
        ("step", "flag"),
        [
            ("snapshot --endpoint http://127.0.0.1:9222", "--endpoint"),
            ("snapshot --target 1", "--target"),
            ("screenshot --url example", "--url"),
            ("console-list --target 2", "--target"),
            ("Page.getFrameTree --target 1", "--target"),
            *[(f"{verb} {flag} value", flag)
              for verb in ("eval 1", "press Enter", "hover --uid AB-1", "type hello",
                           "wait-text ready", "network-get --request-id 1.2")
              for flag in ("--target", "--url", "--endpoint", "--targ", "--end")
              if not (verb.startswith("network-get") and flag == "--url")],
        ],
    )
    def test_a_step_may_not_carry_one(self, step, flag, no_instances):
        with pytest.raises(StepListError) as caught:
            validate(step + "\n")
        assert "must not carry" in str(caught.value)
        assert flag in str(caught.value)

    @pytest.mark.parametrize(
        ("step", "field", "value"),
        [
            ("frames select -- --target", "pattern", "--target"),
            ("frames select -- --target=x", "pattern", "--target=x"),
            ("fill --uid A-1 --text=--target", "text", "--target"),
        ],
    )
    def test_an_option_shaped_value_is_not_an_option(self, step, field, value, no_instances):
        """A frame pattern may look like a flag. The parser knows; a scan does not.

        Scanning the raw tokens of a curated step would refuse
        `frames select -- --target` for carrying `--target`, when `--target`
        is the pattern being selected and nothing is retargeted. The check
        reads the parsed namespace for exactly this reason.
        """
        parsed = validate(step + "\n")[0].args
        assert getattr(parsed, field) == value

    def test_a_flag_the_verb_never_had_is_still_refused(self, no_instances):
        """`snapshot` has no `--url`, so the parser rejects it first.

        Different message, same exit 2 and same nothing executed.
        """
        with pytest.raises(StepListError) as caught:
            validate("snapshot --url ex\n")
        assert "--url" in str(caught.value)

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

    @pytest.mark.parametrize("phrase", [
        "snapshot only-01", "eval only-01 1", "press only-01 Enter",
        "hover only-01 --uid AB-1", "type only-01 hello", "type only-01 --file f",
        "wait-text only-01 ready", "network-get only-01 --request-id 1.2",
    ])
    def test_the_instance_as_a_positional_is_caught(self, one_instance, phrase):
        """`snapshot only-01` parses: `instance` is a positional on every verb."""
        with pytest.raises(StepListError) as caught:
            validate(phrase + "\n")
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

    def test_frames_without_its_sub_action_is_refused(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("frames\n")
        assert "one sub-action" in str(caught.value)

    def test_click_and_fill_both_need_a_uid(self, no_instances):
        for step in ("click", "fill --text hello"):
            with pytest.raises(StepListError) as caught:
                validate(step + "\n")
            assert "requires --uid" in str(caught.value)

    @pytest.mark.parametrize(
        ("step", "fragment"),
        [
            ("screencast --dir /tmp/x --duration 0", "greater than 0"),
            ("screencast --dir /tmp/x --duration -1", "greater than 0"),
            ("screencast --dir /tmp/x --format gif", "'jpeg' or 'png'"),
            ("detect --wait nan", "finite"),
            ("detect --wait inf", "finite"),
        ],
    )
    def test_a_value_that_cannot_run_is_caught_before_step_one(
        self, step, fragment, no_instances
    ):
        """These need no browser, so they belong at validation.

        Each is already a usage error for the bare command. Reached only
        inside the verb, they would fire after earlier steps had driven the
        browser, which is the whole-list guarantee broken.
        """
        with pytest.raises(StepListError) as caught:
            validate("snapshot\n" + step + "\n")
        assert fragment in str(caught.value)
        assert "line 2" in str(caught.value)

    def test_a_good_screencast_and_detect_still_pass(self, no_instances):
        assert len(validate("screencast --dir /tmp/x --duration 2 --format png\ndetect --wait 3\n")) == 2


class TestValidationReachesTheCLIsOwnCode:
    """Proving reuse, not resemblance.

    A test that compares error text passes just as well against a copy, and
    a copy is exactly what RFC-03 forbids. These replace the function and
    assert validation notices.
    """

    def test_the_parser_is_cli_build_parser(self, monkeypatch, no_instances):
        called = []
        real = cli.build_parser

        def spy():
            called.append(True)
            return real()

        monkeypatch.setattr(cli, "build_parser", spy)
        validate("snapshot\ndetect\n")
        assert len(called) == 2, "validation did not go through cli.build_parser"

    def test_the_preconditions_are_cli_check_preconditions(self, monkeypatch, no_instances):
        seen = []
        monkeypatch.setattr(cli, "check_preconditions", lambda args, **_: seen.append(args.command))
        validate("snapshot\nclick\n")
        assert seen == ["snapshot", "click"], (
            "validation did not go through cli.check_preconditions; a step it "
            "should have rejected was accepted because the real one never ran"
        )

    def test_run_reaches_the_same_function(self, monkeypatch):
        """The other half: the live CLI path uses it too."""
        seen = []
        monkeypatch.setattr(cli, "check_preconditions", lambda args: seen.append(args.command))
        with contextlib.suppress(Exception):
            cli._run(argparse.Namespace(command="cleanup"))
        assert seen == ["cleanup"]


class TestTheRegistryIsReadOnce:
    """It is mutable, and another process can change it mid-validation."""

    def test_one_read_for_a_whole_list(self, monkeypatch):
        reads = []
        monkeypatch.setattr(
            "browser_tools.lifecycle.read_instances",
            lambda **_: (reads.append(1), [])[1],
        )
        validate("snapshot\ndetect\nwait-idle\nPage.getFrameTree\n")
        assert len(reads) == 1, f"the registry was read {len(reads)} times"


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

    @pytest.mark.parametrize(
        "written",
        ["""Target.createTarget '{"url": "about:blank"}'""", "Target.createTarget"],
    )
    def test_the_step_carries_the_background_the_guard_adds(self, written, no_instances):
        """`guard_focus` returns normalised params, and the Step keeps them.

        Discarding the return value leaves a Step whose params say nothing
        about `background`. Sent as-is, Chrome's default is foreground, and
        the new tab raises the browser over the user's work: the exact thing
        the guard exists to stop.
        """
        step = validate(written + "\n")[0]
        assert step.params is not None
        assert step.params["background"] is True

    def test_an_explicit_background_true_survives(self, no_instances):
        step = validate("""Target.createTarget '{"background": true}'\n""")[0]
        assert step.params == {"background": True}


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

    def test_the_first_failing_step_is_the_one_reported(self, no_instances):
        with pytest.raises(StepListError) as caught:
            validate("click\nfill\n")
        assert "line 1" in str(caught.value)

    def test_a_line_that_will_not_split_is_reported_before_any_step(self, no_instances):
        """Splitting happens for the whole source first, so it wins.

        `click` on line 1 is already missing its UID, and the unclosed quote
        on line 2 is what gets reported. Both refuse the run and execute
        nothing, so the guarantee holds; the order between the two classes
        is what this pins.
        """
        with pytest.raises(StepListError) as caught:
            validate("click\nfill --text '\n")
        assert "line 2" in str(caught.value)

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
