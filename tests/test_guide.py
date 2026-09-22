"""`bt guide` is the complete manual, and this fails the build when it is not (#100).

The guide is the only documentation an agent reads before driving a browser
with this tool. A verb it does not mention is a verb an agent does not know
exists, and the failure is invisible: the agent simply never uses it, or uses
it wrongly. So the completeness check enumerates the verbs **from the parser**,
never from a list kept beside it -- a hand-maintained list drifts in exactly
the same way the manual does, and then agrees with it.

Two directions are proven: the check passes on the shipped manual, and it fails
when an entry is taken out of it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

import pytest

from browser_tools import cli, lifecycle


def verb_phrases(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb phrase the parser accepts, as the manual would spell it.

    Walks ``_SubParsersAction`` only. ``--engine``'s ``choices`` are not
    sub-actions, and reading them as verbs is how a naive enumeration ends up
    demanding a "launch chrome" entry.
    """
    phrases: list[str] = []
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if not isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            continue
        for name, subparser in action.choices.items():
            phrase = f"{prefix}{name}".strip()
            nested = verb_phrases(subparser, prefix=f"{phrase} ")
            phrases.extend(nested or [phrase])
    return phrases


def flatten(text: str) -> str:
    """Collapse every run of whitespace, so line wrapping is not a contract.

    The manual is wrapped for a human reader, so a rule it states can sit
    across two lines. What must hold is that the rule is stated, not where the
    wrap fell, and a test that pins the wrap turns every reflow into a failure.
    """
    return re.sub(r"\s+", " ", text)


def undocumented(manual: str, phrases: list[str]) -> list[str]:
    """Every verb phrase the manual does not mention."""
    flat = flatten(manual)
    return [phrase for phrase in phrases if phrase not in flat]


@pytest.fixture
def manual() -> str:
    return lifecycle.guide_text()


@pytest.fixture
def flat() -> str:
    """The manual with its line wrapping collapsed, for matching prose."""
    return flatten(lifecycle.guide_text())


@pytest.fixture
def phrases() -> list[str]:
    return verb_phrases(cli.build_parser())


# --------------------------------------------------------------------------- #
# The enforcing check
# --------------------------------------------------------------------------- #


class TestEveryVerbIsDocumented:
    def test_the_enumeration_comes_from_the_parser(self, phrases):
        """Not from a list beside it, which would drift the same way."""
        assert "snapshot" in phrases
        assert "frames select" in phrases
        assert "profile migrate" in phrases
        assert "window-border" in phrases

    def test_the_enumeration_does_not_mistake_flag_choices_for_verbs(self, phrases):
        """`--engine chrome` is a flag value, not a verb phrase."""
        assert "launch chrome" not in phrases
        assert "launch camoufox" not in phrases
        assert "launch" in phrases

    def test_every_verb_has_an_entry(self, manual, phrases):
        missing = undocumented(manual, phrases)
        assert missing == [], f"undocumented verbs: {missing}"

    @pytest.mark.parametrize(
        "phrase",
        [
            "launch", "status", "stop", "cleanup", "guide", "window-border",
            "help", "attach", "wait", "console-list", "network-list",
            "snapshot", "click", "fill", "wait-idle", "wait-stable", "detect",
            "frames list", "frames select", "frames reset", "storage get",
            "screenshot", "screencast",
            "profile list", "profile delete", "profile migrate",
        ],
    )
    def test_each_known_verb_is_there_by_name(self, flat, phrase):
        assert phrase in flat

    def test_the_check_fails_when_an_entry_is_removed(self, phrases):
        """The other direction: a check that cannot fail proves nothing."""
        mutilated = lifecycle.guide_text().replace("wait-stable", "")
        assert undocumented(mutilated, phrases) == ["wait-stable"]

    def test_the_check_fails_for_a_verb_added_without_documentation(self, manual):
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction)  # pyright: ignore[reportPrivateUsage]
        )
        subparsers.add_parser("teleport", help="a verb nobody wrote down")
        assert undocumented(manual, verb_phrases(parser)) == ["teleport"]


# --------------------------------------------------------------------------- #
# Every deliberate refusal, with its exit code
# --------------------------------------------------------------------------- #


class TestEveryRefusalAppearsWithItsExitCode:
    @pytest.mark.parametrize(
        "refusal",
        [
            "Target.activateTarget and Page.bringToFront are refused (exit 2)",
            "background:false is refused (exit 2)",
            "fails (exit 1)",
            "Browser.close and Browser.crash are refused",
            "--endpoint with --profile is refused",
            "mutually exclusive; giving both is a usage error (exit 2)",
            "bare token before -- is a usage error (exit 2)",
            "Naming the instance twice is a usage error (exit 2)",
            "--dir is required (exit 2)",
            "refused (exit 1) naming the holder",
        ],
    )
    def test_the_refusal_is_stated_with_its_code(self, flat, refusal):
        assert refusal in flat

    def test_both_exit_codes_are_defined(self, flat):
        assert "Exit 1" in flat
        assert "Exit 2" in flat
        assert "Exit 0" in flat

    def test_the_three_refusal_classes_each_name_a_remedy(self, flat):
        # The focus guard: open a second window instead of raising one.
        assert '"newWindow": true' in flat
        # External endpoints: forward a remote browser to loopback.
        assert "ssh -L 9222:127.0.0.1:9222 <host>" in flat
        # Profile exclusivity: stop the holder.
        assert "bt stop web-01" in flat


# --------------------------------------------------------------------------- #
# The five things the RFC requires by name
# --------------------------------------------------------------------------- #


class TestTheRequiredContent:
    def test_the_login_walkthrough_runs_end_to_end(self, manual):
        walkthrough = flatten(manual[manual.index("PROFILES AND LOGIN"):])
        for step in (
            "bt launch --profile shopify-admin",
            "bt Page.navigate",
            "log in yourself",
            "bt stop",
            "already signed in",
        ):
            assert step in walkthrough
        assert walkthrough.index("bt launch --profile shopify-admin") < walkthrough.index(
            "bt stop"
        )

    def test_it_names_the_resolved_profile_root(self, flat):
        assert "$BROWSER_TOOLS_PROFILES_DIR" in flat
        assert "$XDG_DATA_HOME/browser-tools/profiles" in flat
        assert "~/.local/share/browser-tools/profiles" in flat

    def test_the_uid_rule_is_stated_once_as_one_rule(self, manual, flat):
        assert "A UID is valid until the page navigates" in flat
        assert "take a new snapshot" in flat
        assert "a fresh snapshot invalidates nothing" in flat
        assert "unnecessary" in flatten(manual[manual.index("THE UID RULE"):])

    def test_the_endpoint_rules_say_the_browser_is_not_registered(self, manual):
        section = flatten(manual[manual.index("EXTERNAL BROWSERS"):])
        assert "THE BROWSER IS NOT REGISTERED" in section
        assert "`status` does not list it" in section
        assert "LOOPBACK ONLY" in section
        assert "log in by hand" in section

    def test_a_profile_and_a_fingerprint_profile_are_distinguished(self, manual):
        section = flatten(manual[manual.index("PROFILES AND LOGIN"):])
        assert "A fingerprint profile is a different thing" in section
        assert "--fingerprint" in section
        assert "stores no login" in section

    def test_storage_key_is_described_as_a_frame_selector(self, flat):
        assert "It is NOT a cookie name or a local-storage key" in flat
        assert "frame URL pattern to select" in flat


# --------------------------------------------------------------------------- #
# Non-obvious contracts, as rules rather than examples
# --------------------------------------------------------------------------- #


class TestTheNonObviousContracts:
    @pytest.mark.parametrize(
        "rule",
        [
            "1-based index into the page targets sorted by target ID",
            "read as an index only when every character is a digit",
            "The sort is normative",
            "A profile is held by at most one live instance",
            "Only JS-solvable challenges auto-retry",
            "subscribe before enabling their domain",
            "substring test against the event's whole JSON serialization",
            "--timeout 0 means no deadline",
            "It reports the registry",
            "never on age",
            "the frame buffer belongs to the process that captured it",
            "A bare leading token is an instance name when the registry knows it",
        ],
    )
    def test_the_contract_is_stated_as_a_rule(self, flat, rule):
        assert rule in flat


# --------------------------------------------------------------------------- #
# The manual describes the surface that exists
# --------------------------------------------------------------------------- #


class TestNoEntryDescribesSomethingRemoved:
    @pytest.mark.parametrize(
        "gone",
        [
            "screencast start",
            "screencast stop",
            "MCP",
            "daemon",
            "chrome-devtools-mcp",
            "/tmp/browser-tools-profiles/shopify",
            "BROWSER_TOOLS_ALLOW_REMOTE",
        ],
    )
    def test_the_manual_does_not_mention_it(self, flat, gone):
        assert gone not in flat

    def test_the_old_profile_root_appears_only_as_history(self, manual):
        """It has to be named, so a reader knows what moved."""
        flat = flatten(manual)
        assert flat.count("/tmp/browser-tools-profiles") == 1
        assert "It used to be /tmp/browser-tools-profiles" in flat


# --------------------------------------------------------------------------- #
# The manual says what the tool is for, not only how each verb is spelled
# --------------------------------------------------------------------------- #


class TestTheManualSaysWhatTheToolIsFor:
    """A reference that never states its purpose leaves an agent guessing.

    The verb list tells a reader how to spell a command. It does not tell
    them whether this tool is the right one for the job in front of them,
    which is the question asked first.
    """

    def test_there_is_a_purpose_section(self, flat):
        assert "WHAT THIS IS FOR" in flat

    @pytest.mark.parametrize(
        "claim",
        [
            "Work behind a login",
            "Pages that resist automation",
            "the accessibility tree",
            "It is not an HTTP client",
        ],
    )
    def test_the_purpose_section_names_the_fit(self, flat, claim):
        assert claim in flat

    def test_it_warns_that_per_invocation_cost_is_real(self, flat):
        assert "about 70 to 85 ms" in flat


# --------------------------------------------------------------------------- #
# The manual says what is reachable without a curated verb
# --------------------------------------------------------------------------- #


class TestTheManualCoversTheUncuratedSurface:
    """Thirteen curated verbs are not the capability surface.

    An agent that reads only the verb list concludes it cannot upload a
    file or set a viewport. Both are one CDP call away. Every recipe named
    here was run against a live browser; see docs/capability-verification.md.
    """

    def test_there_is_a_section_for_it(self, flat):
        assert "NO CURATED VERB? SEND THE PROTOCOL" in flat

    @pytest.mark.parametrize(
        "method",
        [
            "DOM.setFileInputFiles",
            "Input.insertText",
            "Emulation.setDeviceMetricsOverride",
            "Network.getCookies",
            "Network.setCookie",
            "Page.printToPDF",
            "Target.getTargets",
            "Input.dispatchDragEvent",
        ],
    )
    def test_the_recipe_names_its_method(self, flat, method):
        assert method in flat

    def test_upload_uses_the_backend_node_id_from_a_uid(self, flat):
        """A nodeId does not survive an invocation; a backendNodeId does."""
        assert '"backendNodeId": 32' in flat
        assert "the number after the dash in its UID" in flat

    def test_it_says_the_passthrough_does_not_wrap_the_result(self, flat):
        assert "with nothing wrapped" in flat


# --------------------------------------------------------------------------- #
# The manual says what breaks when work is split across two invocations
# --------------------------------------------------------------------------- #


class TestTheManualStatesWhatDoesNotCarry:
    """The failures that cost the most are the ones nothing warns about.

    Each of these was reproduced against a live browser before it was
    written down, with the exact error the browser returns.
    """

    def test_there_is_a_section_for_it(self, flat):
        assert "WHAT DOES NOT CARRY BETWEEN INVOCATIONS" in flat

    @pytest.mark.parametrize(
        "failure",
        [
            "Could not find node with given id",
            "Profiler is not enabled",
            "No dialog is showing",
        ],
    )
    def test_the_real_error_text_is_quoted(self, flat, failure):
        assert failure in flat

    def test_page_state_and_session_state_are_distinguished(self, flat):
        assert "Page state" in flat
        assert "Session state does not" in flat

    def test_frame_selection_is_listed_with_key_as_its_remedy(self, flat):
        section = flat[flat.index("WHAT DOES NOT CARRY BETWEEN INVOCATIONS"):]
        assert "frames select" in section
        assert "--key" in section


# --------------------------------------------------------------------------- #
# What `guide` itself prints
# --------------------------------------------------------------------------- #


class TestTheSecondBinaryIsDocumented:
    """`browser-tools-profiler` ships beside `bt` and the manual omitted it.

    The manual opens by claiming to be everything a reader needs. It
    described one of the two commands the install puts down, so CPU
    profiling looked impossible when it is one command away.
    """

    def test_the_manual_names_it_in_the_opening(self, flat):
        opening = flat[: flat.index("WHAT THIS IS FOR")]
        assert "browser-tools-profiler" in opening

    def test_there_is_a_section_for_it(self, flat):
        assert "CPU PROFILING" in flat

    @pytest.mark.parametrize("piece", ["timed", "watch", "--threshold", "--duration"])
    def test_the_section_covers_its_surface(self, flat, piece):
        section = flat[flat.index("CPU PROFILING"):]
        assert piece in section

    def test_it_says_the_port_is_not_an_instance_name(self, flat):
        section = flat[flat.index("CPU PROFILING"):]
        assert "takes a debug port, not an instance name" in section

    def test_every_installed_console_script_is_documented(self):
        """Read the entry points from pyproject, never from a list here."""
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
        manual = flatten(lifecycle.guide_text())
        missing = [name for name in scripts if name not in manual]
        assert missing == [], f"console scripts with no manual entry: {missing}"


class TestTheVersionFlagIsDocumented:
    """`--version` exists so a stale build on PATH is one command to find.

    A copy from an older install answers every verb and answers some of
    them differently. Before this flag the only way to tell was to run a
    verb and recognise the shape of its output.
    """

    def test_the_manual_mentions_it(self, flat):
        assert "`bt --version` prints the installed version" in flat

    def test_the_manual_says_why_to_check_it(self, flat):
        assert "older copy earlier on PATH" in flat

    def test_the_flag_prints_the_installed_version(self):
        from importlib import metadata

        result = subprocess.run(
            [sys.executable, "-m", "browser_tools.cli", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert metadata.version("browser-tools") in result.stdout

    def test_it_is_not_a_verb_and_needs_no_verb_entry(self, phrases):
        """A flag, so the verb enumeration must not demand a section for it."""
        assert "--version" not in phrases


class TestGuidePrintsPlainText:
    def test_it_prints_plain_text_and_nothing_on_stderr(self):
        result = subprocess.run(
            [sys.executable, "-m", "browser_tools.cli", "guide"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stderr == ""
        assert result.stdout.startswith("browser-tools / bt")

    def test_it_is_not_json(self):
        import json

        with pytest.raises(json.JSONDecodeError):
            json.loads(lifecycle.guide_text())

    def test_it_ships_as_package_data(self):
        """An installed wheel must carry it; a missing file is an empty manual."""
        from importlib import resources

        path = resources.files("browser_tools").joinpath(lifecycle.GUIDE_FILENAME)
        assert path.is_file()
        assert len(lifecycle.guide_text()) > 5000


class TestTheStepSurfaceIsNotHandWritten:
    """The Manual's `run` section lists which verbs are steps and which are not.

    Both lists are prose, and prose drifts. `test_guide.py` already fails the
    build when a verb has no entry anywhere; these fail it when the step
    section disagrees with `step_list`, which is what a caller reads before
    writing a Step List and what `validate` actually enforces.
    """

    @staticmethod
    def _between(start: str, end: str) -> str:
        """The manual text between two markers, as one line.

        Scoped to the enumerating sentence rather than the whole section. A
        looser slice makes the check meaningless: `run` appears as an ordinary
        word all over this section, so a substring test for it passes whether
        or not the excluded list names it. Verified by removing `run itself`
        from the list and watching the wider check still pass.
        """
        text = lifecycle.guide_text()
        begin = text.index(start)
        return " ".join(text[begin : text.index(end, begin)].split())

    def test_every_step_verb_is_named_as_one(self):
        from browser_tools import step_list

        listed = self._between("Steps may be:", "A step may")
        missing = sorted(
            verb for verb in step_list.STEP_VERBS if not re.search(rf"\b{verb}\b", listed)
        )
        assert not missing, (
            f"{missing} can be a step, and the manual's list of steps does not "
            f"say so. A caller reading it would not know the verb is available.\n"
            f"The list reads: {listed}"
        )

    def test_every_excluded_verb_is_named_as_excluded(self):
        from browser_tools import step_list

        listed = self._between("attach is not a step", "Any of these")
        missing = sorted(
            verb for verb in step_list.EXCLUDED_VERBS if not re.search(rf"\b{verb}\b", listed)
        )
        assert not missing, (
            f"{missing} is refused as a step with exit 2, and the manual does "
            f"not say so. The caller finds out by running the list.\n"
            f"The list reads: {listed}"
        )

    def test_the_two_lists_do_not_overlap(self):
        from browser_tools import step_list

        overlap = step_list.STEP_VERBS & set(step_list.EXCLUDED_VERBS)
        assert not overlap, f"{sorted(overlap)} is both a step and not a step"


class TestSixVerbsManual:
    @pytest.mark.parametrize('verb', ['eval','press','hover','type','wait-text','network-get'])
    def test_every_new_verb_has_a_worked_example(self, manual, verb):
        assert f'bt {verb} ' in manual

    def test_key_recipe_uses_press(self, manual):
        assert 'bt press Enter' in manual
        assert 'bt Input.dispatchKeyEvent' not in manual
        assert 'bt Runtime.evaluate' not in manual

    def test_distinguishes_waits_and_network_windows(self, flat):
        for text in ['has network activity stopped?', 'has the DOM stopped changing?',
                     'is this text here?', 'no key events',
                     # Was 'not a buffer', which stopped being true when the run
                     # took ownership of the Network domain: a run does buffer now.
                     # What still distinguishes them is whose window each one sees.
                     'see only events in their own windows',
                     "network-get also sees the run's buffer",
                     # Was 'five seconds', the old fixed window. The run now owns
                     # the Network domain, so a step reads traffic an earlier step
                     # caused, and the standalone window matches network-list.
                     'default 2, matching network-list',
                     'without reloading or losing page state',
                     'bodyOmitted', '--response-file']:
            assert text in flat
