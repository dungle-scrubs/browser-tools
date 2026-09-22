"""The version 6 verb grammar (#99).

Two disagreements between the shipped CLI and the version 6 spec, both settled
by the driving dev while the RFC was revised.

**Screencast is one bounded capture in one invocation.** The frame buffer is
process-local, so a ``screencast stop`` in a second CLI process could never
reach the frames a ``screencast start`` in the first one buffered: the verb
pair dispatched correctly and could not work. A detached recorder process was
considered and declined.

**A leading ``INSTANCE`` binds the sub-action verbs too**, so ``bt guide``
carries no grammar exception. ``frames select [INSTANCE] PATTERN`` cannot be
read with a single bare token, so the instance moves ahead of the verb phrase
and is disambiguated by registry lookup, exactly as the raw protocol line
already does it.
"""

from __future__ import annotations

import pytest

from browser_tools import cli, curated, lifecycle
from browser_tools.core import registry as core_registry


def _entry(port: int = 9222) -> dict:
    return {
        "port": port,
        "pid": 2_000_000_000,
        "browser_version": "Chrome/1",
        "user_data_dir": "",
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": None,
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """An isolated registry the CLI reads through the environment."""
    path = str(tmp_path / "registry.json")
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", path)

    def _seed(**entries: dict) -> str:
        core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
            dict(entries), path
        )
        return path

    _seed()
    return _seed


@pytest.fixture
def captured(monkeypatch):
    """Record the instance each curated verb was called with, and run nothing."""
    seen: dict[str, object] = {}

    def _record(name):
        def _fn(**kwargs):
            seen.clear()
            seen["verb"] = name
            seen.update(kwargs)
            return {"ok": name}

        return _fn

    for name in (
        "snapshot", "click", "fill", "wait_idle", "wait_stable", "detect",
        "frames_list", "frames_select", "frames_reset", "storage_get",
        "screenshot", "screencast",
    ):
        monkeypatch.setattr(curated, name, _record(name))
    return seen


# --------------------------------------------------------------------------- #
# Screencast: one bounded capture
# --------------------------------------------------------------------------- #


class TestScreencastIsOneInvocation:
    def test_the_verb_takes_dir_duration_format_and_max_frames(self, registry, captured):
        registry(**{"only-01": _entry()})
        assert cli.main(
            ["screencast", "--dir", "/tmp/cast", "--duration", "2.5",
             "--format", "png", "--max-frames", "40"]
        ) == 0
        assert captured["verb"] == "screencast"
        assert captured["out_dir"] == "/tmp/cast"
        assert captured["duration"] == 2.5
        assert captured["fmt"] == "png"
        assert captured["max_frames"] == 40

    def test_dir_is_required(self, registry, captured, capsys):
        registry(**{"only-01": _entry()})
        assert cli.main(["screencast"]) == 2
        assert "requires --dir DIR" in capsys.readouterr().err

    def test_the_bounds_have_defaults(self, registry, captured):
        registry(**{"only-01": _entry()})
        cli.main(["screencast", "--dir", "/tmp/cast"])
        assert captured["duration"] == curated.DEFAULT_SCREENCAST_DURATION_SECONDS
        assert captured["max_frames"] == curated.DEFAULT_SCREENCAST_MAX_FRAMES
        assert captured["fmt"] == "jpeg"

    @pytest.mark.parametrize("action", ["start", "stop"])
    def test_the_old_verb_pair_is_gone(self, action, registry, captured, capsys):
        registry(**{"only-01": _entry()})
        assert cli.main(["screencast", action, "--dir", "/tmp/cast"]) == 2
        assert captured == {}

    @pytest.mark.parametrize("action", ["start", "stop"])
    def test_the_removal_names_the_new_form(self, action, registry, captured, capsys):
        registry(**{"only-01": _entry()})
        cli.main(["screencast", action])
        err = capsys.readouterr().err
        assert "takes no sub-action" in err
        assert "bt screencast --dir DIR" in err

    def test_no_screencast_code_path_starts_a_process(self):
        """Screencast MUST NOT own a long-lived process (RFC-01 v6)."""
        import inspect

        source = inspect.getsource(curated)
        body = source[source.index("def screencast("):source.index("def _capture_screenshot")]
        for spawner in ("subprocess", "Popen", "fork", "daemon=True", "Thread("):
            assert spawner not in body, f"screencast reached for {spawner}"


class TestPendingAcksAreHeld:
    def test_the_recorder_holds_every_in_flight_ack(self):
        """The event loop holds only weak references to a bare task."""
        from browser_tools.screencast import ScreencastRecorder

        recorder = ScreencastRecorder()
        assert isinstance(recorder._acks, set)  # pyright: ignore[reportPrivateUsage]

    def test_the_frame_count_is_readable_during_a_capture(self):
        from browser_tools.screencast import ScreencastRecorder

        recorder = ScreencastRecorder()
        assert recorder.frame_count == 0


# --------------------------------------------------------------------------- #
# The leading INSTANCE token
# --------------------------------------------------------------------------- #

#: Every sub-action verb phrase, with the instance ahead of it.
SUB_ACTION_PHRASES = [
    (["frames", "list"], "frames_list"),
    (["frames", "select", "checkout"], "frames_select"),
    (["frames", "reset"], "frames_reset"),
    (["storage", "get"], "storage_get"),
    (["screencast", "--dir", "/tmp/cast"], "screencast"),
]


class TestALeadingInstanceBindsSubActionVerbs:
    @pytest.mark.parametrize(("phrase", "verb"), SUB_ACTION_PHRASES, ids=lambda v: str(v))
    def test_the_instance_leads_the_verb_phrase(self, phrase, verb, registry, captured):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        assert cli.main(["web-01", *phrase]) == 0
        assert captured["verb"] == verb
        assert captured["instance"] == "web-01"

    @pytest.mark.parametrize(("phrase", "verb"), SUB_ACTION_PHRASES, ids=lambda v: str(v))
    def test_omitting_it_with_one_instance_works(self, phrase, verb, registry, captured):
        registry(**{"only-01": _entry()})
        assert cli.main(phrase) == 0
        assert captured["instance"] is None

    def test_frames_select_still_takes_a_url_pattern(self, registry, captured):
        registry(**{"web-01": _entry()})
        cli.main(["web-01", "frames", "select", "pay.example.com"])
        assert captured["pattern"] == "pay.example.com"

    def test_a_pattern_is_not_mistaken_for_an_instance(self, registry, captured):
        """`frames` is not a registry name, so it is the verb."""
        registry(**{"only-01": _entry()})
        assert cli.main(["frames", "select", "checkout"]) == 0
        assert captured["instance"] is None
        assert captured["pattern"] == "checkout"

    def test_a_registry_name_that_collides_with_a_pattern_is_the_instance(
        self, registry, captured
    ):
        registry(**{"checkout": _entry()})
        assert cli.main(["checkout", "frames", "select", "checkout"]) == 0
        assert captured["instance"] == "checkout"
        assert captured["pattern"] == "checkout"


class TestALeadingInstanceBindsEveryBrowserVerb:
    @pytest.mark.parametrize(
        ("phrase", "verb"),
        [
            (["snapshot"], "snapshot"),
            (["click", "--uid", "AAA-1"], "click"),
            (["fill", "--uid", "AAA-1", "--text", "x"], "fill"),
            (["wait-idle"], "wait_idle"),
            (["wait-stable"], "wait_stable"),
            (["detect"], "detect"),
            (["screenshot"], "screenshot"),
        ],
        ids=lambda v: str(v),
    )
    def test_the_instance_may_lead_the_verb(self, phrase, verb, registry, captured):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        assert cli.main(["web-01", *phrase]) == 0
        assert captured["verb"] == verb
        assert captured["instance"] == "web-01"

    def test_naming_the_instance_twice_is_a_usage_error(self, registry, captured, capsys):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        assert cli.main(["web-01", "snapshot", "web-02"]) == 2
        assert "named twice" in capsys.readouterr().err

    def test_the_endpoint_flag_survives_the_split(self, registry, captured):
        registry(**{"web-01": _entry()})
        assert cli.main(["web-01", "frames", "list", "--endpoint", "http://127.0.0.1:9222"]) == 0
        assert captured["endpoint"] == "http://127.0.0.1:9222"

    def test_the_endpoint_flag_survives_a_leading_position(self, registry, captured):
        registry(**{"web-01": _entry()})
        assert cli.main(
            ["--endpoint", "http://127.0.0.1:9222", "web-01", "frames", "list"]
        ) == 0
        assert captured["instance"] == "web-01"
        assert captured["endpoint"] == "http://127.0.0.1:9222"


class TestALeadingInstanceBindsTheFreeFormVerbs:
    """`help` and `attach` read their instance out of a positional list."""

    def test_help_takes_a_leading_instance(self, registry, monkeypatch):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "browser_tools.passthrough.run_help",
            lambda instance, query, **kw: seen.update(instance=instance, query=query),
        )
        assert cli.main(["web-01", "help", "Page.navigate"]) == 0
        assert seen == {"instance": "web-01", "query": "Page.navigate"}

    def test_attach_takes_a_leading_instance(self, registry, monkeypatch):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "browser_tools.events.run_attach", lambda **kw: seen.update(kw)
        )
        assert cli.main(["web-01", "attach", "+Page.loadEventFired"]) == 0
        assert seen["instance"] == "web-01"
        assert seen["events"] == ["Page.loadEventFired"]

    def test_naming_it_twice_is_a_usage_error(self, registry, capsys):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        assert cli.main(["web-01", "attach", "web-02", "+Page.loadEventFired"]) == 2
        assert "named twice" in capsys.readouterr().err

    def test_the_inline_form_still_works(self, registry, monkeypatch):
        registry(**{"web-01": _entry(), "web-02": _entry(port=9223)})
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "browser_tools.events.run_attach", lambda **kw: seen.update(kw)
        )
        assert cli.main(["attach", "web-02", "+Page.loadEventFired"]) == 0
        assert seen["instance"] == "web-02"


class TestTheRawProtocolLineIsUnaffected:
    def test_an_instance_before_a_domain_method_still_routes_to_passthrough(
        self, registry, monkeypatch, capsys
    ):
        registry(**{"web-01": _entry()})
        sent: dict[str, object] = {}

        def _send(**kwargs):
            sent.update(kwargs)
            return {"ok": True}

        monkeypatch.setattr("browser_tools.passthrough.send", _send)
        assert cli.main(["web-01", "Page.navigate", '{"url":"x"}']) == 0
        assert sent["instance"] == "web-01"
        assert sent["method"] == "Page.navigate"

    def test_a_bare_domain_method_still_routes_to_passthrough(
        self, registry, monkeypatch
    ):
        registry(**{"web-01": _entry()})
        sent: dict[str, object] = {}
        monkeypatch.setattr(
            "browser_tools.passthrough.send", lambda **kw: sent.update(kw) or {"ok": True}
        )
        assert cli.main(["Page.navigate", "{}"]) == 0
        assert sent["instance"] is None

    def test_an_unknown_leading_token_is_still_an_unknown_verb(self, registry):
        registry(**{"web-01": _entry()})
        with pytest.raises(SystemExit) as exc:
            cli.main(["nonsense", "snapshot"])
        assert exc.value.code == 2


class TestTheSettledDisagreementsStand:
    """Three code-versus-spec disagreements settled in the shipped code's favour."""

    @pytest.mark.parametrize("verb", ["console-list", "network-list", "screenshot"])
    def test_url_stays_on_the_list_and_screenshot_verbs(self, verb):
        args = cli.build_parser().parse_args([verb, "--url", "example.com"])
        assert args.url == "example.com"

    def test_frames_select_takes_a_pattern_not_an_index(self):
        args = cli.build_parser().parse_args(["frames", "select", "pay.example"])
        assert args.pattern == "pay.example"

    def test_guide_and_help_are_not_json(self, capsys):
        assert cli.main(["guide"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("browser-tools")


class TestInstanceIsRegistered:
    def test_a_known_name_is_recognized(self, registry):
        path = registry(**{"web-01": _entry()})
        assert lifecycle.instance_is_registered("web-01", registry_path=path)

    def test_an_unknown_name_is_not(self, registry):
        path = registry(**{"web-01": _entry()})
        assert not lifecycle.instance_is_registered("frames", registry_path=path)


class TestSixVerbGrammar:
    @pytest.mark.parametrize(('argv', 'function'), [
        (['eval', '1+1'], 'eval_js'), (['press', 'Enter'], 'press'),
        (['hover', '--uid', 'AB-1'], 'hover'), (['type', 'hello'], 'type_text'),
        (['type', '--file', 'message.txt'], 'type_text'),
        (['wait-text', 'ready'], 'wait_text'),
        (['network-get', '--request-id', '1.2'], 'network_get'),
    ])
    @pytest.mark.parametrize('placement', ['leading', 'inline', 'omitted'])
    def test_instance_forms(self, argv, function, placement, registry, monkeypatch):
        registry(**{'only-01': _entry()})
        seen = []
        monkeypatch.setattr(curated, function, lambda **kwargs: seen.append(kwargs) or {})
        if placement == 'leading':
            argv = ['only-01', *argv]
        elif placement == 'inline':
            argv = [argv[0], 'only-01', *argv[1:]]
        assert cli.main(argv) == 0
        assert seen[0]['instance'] == (None if placement == 'omitted' else 'only-01')

    @pytest.mark.parametrize('argv', [
        ['eval'], ['press'], ['press','NotAKey'], ['press','Enter','--modifiers','Ctrl'],
        ['type'], ['type','hello','--file','message.txt'], ['hover'], ['wait-text'],
        ['wait-text','ready','--timeout-ms','-1'], ['network-get'],
        ['network-get','--url','api','--request-id','1'],
        ['eval','1','--url','page','--target','1'],
    ])
    def test_invalid_usage_sends_nothing(self, argv, registry, monkeypatch, capsys):
        def forbidden(*args, **kwargs):
            raise AssertionError('usage failure opened a browser')
        monkeypatch.setattr(curated, '_resolve_port', forbidden)
        assert cli.main(argv) == 2
        assert capsys.readouterr().out == ''

    @pytest.mark.parametrize(('argv','valid'), [
        (['press','Unknown'], ['Enter','PageDown','F12']),
        (['press','Enter','--modifiers','Bad'], ['Alt','Control','Meta','Shift']),
    ])
    def test_invalid_names_print_valid_names(self, argv, valid, registry, capsys):
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert all(name in err for name in valid)

    def test_duplicate_instance_refused(self, registry, monkeypatch):
        registry(**{'only-01': _entry()})
        assert cli.main(['only-01','eval','only-01','1']) == 2
