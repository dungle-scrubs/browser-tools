"""``--endpoint URL`` drives an external browser, and writes no registry entry (#97).

The safety property under test is an absence. An external Chrome is the user's
real browser, so its user-data-dir is the real Chrome profile directory, and two
vendored paths ``rmtree`` whatever that field holds. With no registry entry
``stop`` and ``cleanup`` cannot see the browser, so there is nothing to guard --
which is why the tests below assert on the registry file as much as on the port.

Three refusals carry the rest: loopback only with no escape hatch, no
``Browser.close`` / ``Browser.crash``, and no ``--endpoint`` on a registry verb
or beside ``--profile``. Each is exit 2, and each is checked to refuse *before*
anything is sent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from browser_tools import cli, endpoint, lifecycle, passthrough

# --------------------------------------------------------------------------- #
# Endpoint parsing
# --------------------------------------------------------------------------- #


class TestResolveEndpointPort:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:9222",
            "http://127.0.0.1:9222/",
            "https://127.0.0.1:9222",
            "127.0.0.1:9222",
            "http://[::1]:9222",
            "  http://127.0.0.1:9222  ",
        ],
    )
    def test_loopback_forms_resolve_to_the_port(self, url):
        assert endpoint.resolve_endpoint_port(url).port == 9222

    def test_a_bare_host_and_port_is_read_as_http(self):
        assert endpoint.resolve_endpoint_port("127.0.0.1:1234").port == 1234

    @pytest.mark.parametrize(
        "url",
        ["http://192.168.1.50:9222", "http://example.com:9222", "http://10.0.0.1:9222"],
    )
    def test_a_non_loopback_host_is_refused(self, url):
        with pytest.raises(endpoint.EndpointUsageError) as exc:
            endpoint.resolve_endpoint_port(url)
        assert "loopback" in str(exc.value)

    def test_the_refusal_names_the_ssh_tunnel_that_works(self):
        with pytest.raises(endpoint.EndpointUsageError) as exc:
            endpoint.resolve_endpoint_port("http://10.0.0.1:9222")
        message = str(exc.value)
        assert "ssh -L 9222:127.0.0.1:9222" in message
        assert "--endpoint http://127.0.0.1:9222" in message

    def test_localhost_is_refused_because_it_resolves_to_either_or_neither(self):
        with pytest.raises(endpoint.EndpointUsageError):
            endpoint.resolve_endpoint_port("http://localhost:9222")

    def test_there_is_no_escape_hatch_environment_variable(self, monkeypatch):
        """The retired attach handler had one. It does not come back."""
        for name in (
            "BROWSER_TOOLS_ALLOW_REMOTE_ENDPOINT",
            "BROWSER_TOOLS_ALLOW_REMOTE",
        ):
            monkeypatch.setenv(name, "1")
        with pytest.raises(endpoint.EndpointUsageError):
            endpoint.resolve_endpoint_port("http://10.0.0.1:9222")

    def test_a_missing_port_is_refused(self):
        with pytest.raises(endpoint.EndpointUsageError) as exc:
            endpoint.resolve_endpoint_port("http://127.0.0.1")
        assert "no port" in str(exc.value)

    def test_a_non_http_scheme_is_refused(self):
        with pytest.raises(endpoint.EndpointUsageError) as exc:
            endpoint.resolve_endpoint_port("ws://127.0.0.1:9222")
        assert "http" in str(exc.value)


class TestBrowserLifetimeRefusal:
    @pytest.mark.parametrize("method", ["Browser.close", "Browser.crash"])
    def test_the_two_browser_enders_are_refused(self, method):
        with pytest.raises(endpoint.EndpointUsageError) as exc:
            endpoint.refuse_browser_lifetime_method(method)
        assert "--target SPEC" in str(exc.value)

    def test_the_refusal_names_the_remedy_the_manual_names(self):
        """A remedy an agent reads in a diagnostic has to be the working one."""
        from browser_tools import lifecycle

        with pytest.raises(endpoint.EndpointUsageError) as exc:
            endpoint.refuse_browser_lifetime_method("Browser.close")
        assert "Target.closeTarget" in str(exc.value)
        assert "Target.closeTarget" in lifecycle.guide_text()

    @pytest.mark.parametrize(
        "method",
        [
            "Target.closeTarget",
            "Page.navigate",
            "Browser.getVersion",
            "Network.getCookies",
            "Input.dispatchMouseEvent",
        ],
    )
    def test_everything_else_passes(self, method):
        endpoint.refuse_browser_lifetime_method(method)


# --------------------------------------------------------------------------- #
# The registry is never touched
# --------------------------------------------------------------------------- #


class TestTheRegistryStaysOut:
    def test_the_port_comes_from_the_url_with_no_registry_read(self, tmp_path):
        registry = tmp_path / "registry.json"
        assert lifecycle.resolve_cdp_port(None, str(registry), "http://127.0.0.1:9333").port == 9333
        assert not registry.exists()

    def test_an_instance_name_beside_an_endpoint_is_ignored_not_looked_up(self, tmp_path):
        """No lookup happens, so a name the registry has never heard of is fine."""
        registry = tmp_path / "registry.json"
        port = lifecycle.resolve_cdp_port("no-such-instance", str(registry), "http://127.0.0.1:9444")
        assert port.port == 9444
        assert not registry.exists()

    def test_without_an_endpoint_the_registry_still_resolves(self, tmp_path):
        registry = tmp_path / "registry.json"
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle.resolve_cdp_port(None, str(registry), None)


# --------------------------------------------------------------------------- #
# Flag extraction (the raw-protocol line bypasses argparse)
# --------------------------------------------------------------------------- #


class TestEndpointFlagExtraction:
    def test_the_flag_is_pulled_from_the_tail(self):
        remaining, endpoint_url = passthrough.strip_endpoint_flag(
            ["Page.navigate", "{}", "--endpoint", "http://127.0.0.1:9222"]
        )
        assert remaining == ["Page.navigate", "{}"]
        assert endpoint_url == "http://127.0.0.1:9222"

    def test_the_flag_is_pulled_from_the_head(self):
        remaining, endpoint_url = passthrough.strip_endpoint_flag(
            ["--endpoint", "http://127.0.0.1:9222", "Page.navigate", "{}"]
        )
        assert remaining == ["Page.navigate", "{}"]
        assert endpoint_url == "http://127.0.0.1:9222"

    def test_no_flag_leaves_the_argv_alone(self):
        remaining, endpoint_url = passthrough.strip_endpoint_flag(["Page.navigate", "{}"])
        assert remaining == ["Page.navigate", "{}"]
        assert endpoint_url is None

    def test_extract_target_flags_returns_the_endpoint_too(self):
        remaining, target, url, endpoint_url = passthrough.extract_target_flags(
            ["Page.navigate", "{}", "--target", "2", "--endpoint", "http://127.0.0.1:9222"]
        )
        assert remaining == ["Page.navigate", "{}"]
        assert target == "2"
        assert url is None
        assert endpoint_url == "http://127.0.0.1:9222"


# --------------------------------------------------------------------------- #
# Which verbs take the flag
# --------------------------------------------------------------------------- #

#: Every verb the ticket says must accept ``--endpoint``. The sub-action verbs
#: carry it on each leaf, so they are spelled out.
ENDPOINT_VERBS = [
    ["help"],
    ["attach", "+Page.loadEventFired"],
    ["wait", "--event", "Page.loadEventFired"],
    ["console-list"],
    ["network-list"],
    ["snapshot"],
    ["click", "--uid", "AAA-1"],
    ["fill", "--uid", "AAA-1", "--text", "x"],
    ["wait-idle"],
    ["wait-stable"],
    ["detect"],
    ["frames", "list"],
    ["frames", "select", "pattern"],
    ["frames", "reset"],
    ["storage", "get"],
    ["screenshot"],
    ["screencast", "--dir", "/tmp/x"],
]


class TestWhichVerbsAcceptTheFlag:
    @pytest.mark.parametrize("verb", ENDPOINT_VERBS, ids=lambda v: " ".join(v))
    def test_a_browser_driving_verb_parses_the_flag(self, verb):
        args = cli.build_parser().parse_args([*verb, "--endpoint", "http://127.0.0.1:9222"])
        assert args.endpoint == "http://127.0.0.1:9222"

    @pytest.mark.parametrize("verb", ["launch", "status", "stop", "cleanup", "guide", "profile"])
    def test_a_registry_verb_refuses_the_flag_at_exit_2(self, verb, capsys):
        assert cli.main([verb, "--endpoint", "http://127.0.0.1:9222"]) == 2
        assert "does not take --endpoint" in capsys.readouterr().err

    @pytest.mark.parametrize("verb", ["launch", "status", "stop", "cleanup", "guide", "profile"])
    def test_the_refusal_holds_with_the_flag_first(self, verb, capsys):
        assert cli.main(["--endpoint", "http://127.0.0.1:9222", verb]) == 2
        assert "does not take --endpoint" in capsys.readouterr().err

    def test_endpoint_with_profile_is_a_usage_error(self, capsys):
        code = cli.main(["launch", "--profile", "work", "--endpoint", "http://127.0.0.1:9222"])
        assert code == 2
        err = capsys.readouterr().err
        assert "--endpoint and --profile cannot be combined" in err
        assert "bt launch --profile NAME" in err

    def test_the_profile_refusal_wins_over_the_verb_refusal(self, capsys):
        """Both apply to `launch --profile`; the profile message is the useful one."""
        cli.main(["launch", "--profile", "work", "--endpoint", "http://127.0.0.1:9222"])
        assert "cannot be combined" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Refusals happen before anything is sent
# --------------------------------------------------------------------------- #


class TestNothingIsSentBeforeARefusal:
    @pytest.fixture(autouse=True)
    def _explode_on_connect(self, monkeypatch):
        """Any attempt to open a connection fails the test loudly."""

        def _boom(*args, **kwargs):
            raise AssertionError("a refused invocation opened a connection")

        monkeypatch.setattr("browser_tools.one_shot.get_ws_url_async", _boom)

    def test_a_non_loopback_endpoint_sends_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
        code = cli.main(["Page.navigate", '{"url":"x"}', "--endpoint", "http://10.0.0.1:9222"])
        assert code == 2
        assert "loopback" in capsys.readouterr().err

    @pytest.mark.parametrize("method", ["Browser.close", "Browser.crash"])
    def test_a_browser_ender_sends_nothing(self, method, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
        code = cli.main([method, "{}", "--endpoint", "http://127.0.0.1:9222"])
        assert code == 2
        assert "refused over --endpoint" in capsys.readouterr().err

    def test_a_refused_invocation_writes_no_registry_entry(self, tmp_path, monkeypatch):
        registry = tmp_path / "registry.json"
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(registry))
        cli.main(["Browser.close", "{}", "--endpoint", "http://127.0.0.1:9222"])
        assert not registry.exists()


class TestTheBrowserEnderRefusalIsOnlyForEndpoints:
    def test_browser_close_still_reaches_a_registered_instance(self, monkeypatch):
        """The refusal is about a browser this tool does not own, not the method."""
        sent: list[str] = []

        def _fake_resolve(instance, registry_path, endpoint=None):
            return 9222

        monkeypatch.setattr(lifecycle, "resolve_cdp_port", _fake_resolve)

        class _Cdp:
            async def send(self, *, method, params=None, session_id=None):
                sent.append(method)
                return {}

        import contextlib

        @contextlib.asynccontextmanager
        async def _session(port, spec, by, *, external=False):
            yield _Cdp(), "session-1"

        monkeypatch.setattr("browser_tools.passthrough.one_shot_page_session", _session)
        passthrough.send(instance="x", method="Browser.close", params_json=None)
        assert sent == ["Browser.close"]


# --------------------------------------------------------------------------- #
# attach takes the external path, never the vendored one
# --------------------------------------------------------------------------- #


class TestAttachOverAnEndpoint:
    def test_the_vendored_registry_session_is_not_reached(self, monkeypatch):
        """It resolves its port through the registry and then polls that entry."""
        from browser_tools import events as events_module

        def _boom(*args, **kwargs):
            raise AssertionError("--endpoint reached the vendored registry attach")

        monkeypatch.setattr("browser_tools.core.attach.run_attach", _boom)

        taken: list[tuple] = []

        async def _external(port, subscriptions, target_spec, target_by):
            taken.append((port, subscriptions, target_spec, target_by))

        monkeypatch.setattr(events_module, "_attach_external", _external)
        events_module.run_attach(
            instance=None,
            events=["Page.loadEventFired"],
            target="2",
            endpoint="http://127.0.0.1:9222",
        )
        assert taken == [(endpoint.resolve_endpoint_port("http://127.0.0.1:9222"), ["Page.loadEventFired"], "2", "index")]

    def test_an_empty_subscription_list_is_still_a_usage_error(self):
        from browser_tools import events as events_module

        with pytest.raises(passthrough.UsageError):
            events_module.run_attach(
                instance=None, events=[], endpoint="http://127.0.0.1:9222"
            )

    def test_a_non_loopback_endpoint_is_refused_before_any_connection(self):
        from browser_tools import events as events_module

        with pytest.raises(endpoint.EndpointUsageError):
            events_module.run_attach(
                instance=None,
                events=["Page.loadEventFired"],
                endpoint="http://10.0.0.1:9222",
            )


class TestStdinWatchability:
    """``connect_read_pipe`` fails inside a callback, so it is probed first."""

    def test_a_pipe_is_watchable(self, monkeypatch):
        import os

        from browser_tools import events as events_module

        read_fd, write_fd = os.pipe()
        try:
            monkeypatch.setattr(
                "sys.stdin", type("F", (), {"fileno": staticmethod(lambda: read_fd)})()
            )
            assert events_module._stdin_is_watchable() is True
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_a_closed_stdin_is_not_watchable(self, monkeypatch):
        from browser_tools import events as events_module

        class _Closed:
            @staticmethod
            def fileno():
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr("sys.stdin", _Closed())
        assert events_module._stdin_is_watchable() is False

    def test_a_missing_stdin_is_not_watchable(self, monkeypatch):
        from browser_tools import events as events_module

        monkeypatch.setattr("sys.stdin", None)
        assert events_module._stdin_is_watchable() is False


# --------------------------------------------------------------------------- #
# The guide documents the flag
# --------------------------------------------------------------------------- #


class TestTheGuideDocumentsIt:
    def test_the_guide_has_an_external_browsers_section(self):
        guide = lifecycle.guide_text()
        assert "EXTERNAL BROWSERS" in guide
        assert "--endpoint URL" in guide
        assert "ssh -L 9222:127.0.0.1:9222" in guide
        assert "Browser.close and Browser.crash are refused" in guide


# --------------------------------------------------------------------------- #
# Connection diagnosis
# --------------------------------------------------------------------------- #


class TestConnectionDiagnosis:
    def test_an_unheld_port_says_nothing_is_listening(self, monkeypatch):
        monkeypatch.setattr(
            "browser_tools.process_utils.find_listeners_on_port", lambda port: []
        )
        assert "nothing is listening on port 9222" in endpoint.describe_endpoint(9222)

    def test_a_held_port_names_the_pid_profile_and_debug_port(self, monkeypatch):
        monkeypatch.setattr(
            "browser_tools.process_utils.find_listeners_on_port", lambda port: [4242]
        )
        monkeypatch.setattr(
            "browser_tools.process_utils.find_chrome_user_data_dir",
            lambda pid: "/Users/someone/Library/Application Support/Google/Chrome",
        )
        monkeypatch.setattr(
            "browser_tools.process_utils.find_chrome_debug_port", lambda pid: 9222
        )
        monkeypatch.setattr(
            "browser_tools.process_utils.read_process_command",
            lambda pid: "/Applications/Chrome --remote-debugging-port=9222",
        )
        described = endpoint.describe_endpoint(9222)
        assert "pid 4242" in described
        assert "Google/Chrome" in described
        assert "--remote-debugging-port=9222" in described

    def test_several_listeners_are_reported_as_a_collision(self, monkeypatch):
        monkeypatch.setattr(
            "browser_tools.process_utils.find_listeners_on_port", lambda port: [1, 2]
        )
        monkeypatch.setattr(
            "browser_tools.process_utils.find_chrome_user_data_dir", lambda pid: None
        )
        monkeypatch.setattr("browser_tools.process_utils.find_chrome_debug_port", lambda pid: None)
        monkeypatch.setattr("browser_tools.process_utils.read_process_command", lambda pid: "chrome")
        described = endpoint.describe_endpoint(9222)
        assert "collision" in described
        assert "pid 1" in described
        assert "pid 2" in described

    def test_a_failed_external_connection_carries_the_diagnosis(self, monkeypatch):
        from browser_tools import one_shot

        monkeypatch.setattr(
            "browser_tools.process_utils.find_listeners_on_port", lambda port: []
        )
        message = one_shot.connection_failure_message(port=9222, cause=None, external=True)
        assert "--remote-debugging-port=9222" in message
        assert "nothing is listening on port 9222" in message

    def test_the_registry_path_message_is_unchanged(self):
        from browser_tools import one_shot

        message = one_shot.connection_failure_message(port=9222, cause=None)
        assert message == "No browser listening on port 9222. Start one with: bt launch"


# --------------------------------------------------------------------------- #
# A live external browser
# --------------------------------------------------------------------------- #


def _external_chrome_port() -> int | None:
    """The port of an external Chrome to drive, from ``BT_TEST_ENDPOINT_PORT``."""
    import os

    raw = os.environ.get("BT_TEST_ENDPOINT_PORT")
    return int(raw) if raw and raw.isdigit() else None


@pytest.mark.skipif(
    _external_chrome_port() is None,
    reason="set BT_TEST_ENDPOINT_PORT to a Chrome started with --remote-debugging-port",
)
class TestAgainstALiveExternalChrome:
    def test_snapshot_drives_it_and_leaves_no_registry_entry(self, tmp_path, monkeypatch, capsys):
        registry = tmp_path / "registry.json"
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(registry))
        port = _external_chrome_port()
        assert cli.main(["snapshot", "--endpoint", f"http://127.0.0.1:{port}"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload
        assert not Path(registry).exists()

    def test_the_focus_guard_still_refuses_an_activation(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
        port = _external_chrome_port()
        code = cli.main(
            ["Target.activateTarget", "{}", "--endpoint", f"http://127.0.0.1:{port}"]
        )
        assert code != 0
        assert capsys.readouterr().err
