"""The whole login flow against a disposable Chrome and a synthetic form.

Everything here is synthetic: a throwaway headless Chrome (never the
developer's browser), a loopback HTTPS server with a self-signed certificate
serving a hand-written login form, and a fake ``opchain`` on PATH that
answers the relay probe and writes sentinel credentials from a fixed table.
No 1Password account, real credential, or real site is involved.

What this proves that the fake-CDP module cannot: the real ``login_form.js``
against the real DOM (native setters firing framework-visible events,
``requestSubmit`` on the validated button, same-origin POST with overrides),
the real relay recipe end to end, the real loaderId identity across the
navigation the submit causes, and the field-clearing guarantee on both the
navigating and the single-page shapes.

Skipped when no plain Chrome binary (application-bundle launches abort under
repeated test launches) or no openssl for the certificate is available; the
gap is then reported by the worker rather than silently claimed.
"""

from __future__ import annotations

import http.server
import json
import os
import ssl
import subprocess
import textwrap
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING

import pytest

from browser_tools import lifecycle

if TYPE_CHECKING:
    from pathlib import Path

VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"
SENTINEL_USER = "SENTINEL-live-user"
SENTINEL_PASS = "SENTINEL-live-pass"

LOGIN_PAGE = textwrap.dedent(
    """
    <!doctype html>
    <title>sign in</title>
    <form method="post" action="/login">
      <input id="username" type="text" name="u">
      <input id="password" type="password" name="p">
      <button type="submit" id="go">Sign in</button>
    </form>
    """
)

WELCOME_PAGE = textwrap.dedent(
    """
    <!doctype html>
    <title>welcome</title>
    <main data-signed-in>Signed in</main>
    """
)

# A single-page variant shaped like a framework form: state is maintained
# from the fill's input events, the submit handler awaits a real POST that
# carries the pair from that state (never from the DOM), and the success
# marker appears only after the response. The password input handler also
# restores the remembered username into the DOM, the way framework state
# management re-writes fields, and the response handler re-renders both
# fields from state after the response commits.
SPA_PAGE = textwrap.dedent(
    """
    <!doctype html>
    <title>spa sign in</title>
    <form method="post" action="/spa-login" id="theform">
      <input id="username" type="text" name="u">
      <input id="password" type="password" name="p">
      <button type="submit" id="go">Sign in</button>
    </form>
    <script>
      var state = {u: '', p: ''};
      document.getElementById('username').addEventListener('input', function (event) {
        state.u = event.target.value;
      });
      document.getElementById('password').addEventListener('input', function (event) {
        state.p = event.target.value;
        document.getElementById('username').value = state.u;
      });
      document.getElementById('theform').addEventListener('submit', async function (event) {
        event.preventDefault();
        queueMicrotask(function () {
          window.submitClearedBeforeYield =
            document.getElementById('username').value === '' &&
            document.getElementById('password').value === '';
        });
        await Promise.resolve();
        var body = 'u=' + encodeURIComponent(state.u) + '&p=' + encodeURIComponent(state.p);
        await window.fetch('/spa-login', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: body
        });
        window.stateAfterResponse = {u: state.u, p: state.p};
        document.getElementById('username').value = state.u;
        document.getElementById('password').value = state.p;
        var marker = document.createElement('main');
        marker.setAttribute('data-signed-in', '');
        document.body.appendChild(marker);
      });
    </script>
    """
)

# The username and the submit button share one form; the password input
# belongs to a second form. Everything else about both forms is valid, so
# only the common-form check can refuse this page.
SEPARATE_FORM_PAGE = textwrap.dedent(
    """
    <!doctype html>
    <title>split sign in</title>
    <form method="post" action="/login">
      <input id="username" type="text" name="u">
      <button type="submit" id="go">Sign in</button>
    </form>
    <form method="post" action="/login">
      <input id="password" type="password" name="p">
    </form>
    """
)

# The configured success marker, twice, before any login: a page that is
# already signed in with the marker repeated must not slip past the
# already-authenticated refusal.
TWO_MARKERS_PAGE = LOGIN_PAGE + textwrap.dedent(
    """
    <main data-signed-in>one</main>
    <main data-signed-in>two</main>
    """
)



class LoginFormServer:
    """The synthetic site: one form, one POST recorder, one welcome page."""

    def __init__(self, page: str) -> None:
        self.posts: list[dict[str, str]] = []
        self.gets: list[str] = []
        self.page = page
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.gets.append(self.path)
                body = (outer.welcome_page() if self.path == "/welcome" else outer.page).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                data = self.rfile.read(length).decode("utf-8")
                outer.posts.append(
                    {k: v[0] for k, v in urllib.parse.parse_qs(data).items()}
                )
                if self.path == "/spa-login":
                    body = b"<html></html>"
                    self.send_response(200)
                else:
                    body = b""
                    self.send_response(302)
                    self.send_header("Location", "/welcome")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def welcome_page(self) -> str:
        return WELCOME_PAGE

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def origin(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def start(self, cert: Path, key: Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_cert(tmp: Path) -> tuple[Path, Path]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        ["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-nodes",
         "-subj", "/CN=127.0.0.1"],
        capture_output=True,
        check=True,
    )
    return cert, key


def _write_fake_opchain(bin_dir: Path) -> None:
    """The fake relay: answers the probe, records each read, writes values."""
    body = textwrap.dedent(
        f"""
        #!/usr/bin/env python3
        import os, sys
        argv = sys.argv
        fd_text = argv[argv.index("--value-fd") + 1]
        if fd_text == "0":
            sys.stderr.write("opchain: relay: descriptor-standard\\n")
            sys.exit(1)
        ref = argv[argv.index("op") + 2]
        field = ref.rsplit("/", 1)[-1]
        with open({str(bin_dir / 'relay-reads.log')!r}, "a") as fh:
            fh.write(field + "\\n")
        values = {{"username": {SENTINEL_USER!r}, "password": {SENTINEL_PASS!r}}}
        data = values.get(field, "").encode("utf-8")
        os.write(1, data)
        os.write(2, data)
        os.write(int(fd_text), data)
        sys.exit(0)
        """
    ).lstrip("\n")
    path = bin_dir / "opchain"
    path.write_text(body)
    path.chmod(0o755)


def _relay_reads(fake_relay) -> list[str]:
    """The fields the fake opchain was asked for, in order."""
    from pathlib import Path

    log = Path(fake_relay).parent / "bin" / "relay-reads.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.strip()]


def _write_policy(tmp: Path, origin: str, *, username_selector="#username",
                  password_selector="#password", submit_selector="#go",
                  success_selector="[data-signed-in]") -> Path:
    document = {
        "version": 1,
        "identity": "browser",
        "vault": VAULT,
        "accounts": {
            "example": {
                "item": ITEM,
                "origin": origin,
                "username_selector": username_selector,
                "password_selector": password_selector,
                "submit_selector": submit_selector,
                "success_selector": success_selector,
            }
        },
    }
    path = tmp / "logins.json"
    path.write_text(json.dumps(document))
    return path


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """The loopback HTTPS site, on an OS-assigned port."""
    tmp = tmp_path_factory.mktemp("login-site")
    cert, key = _make_cert(tmp)
    server = LoginFormServer(LOGIN_PAGE)
    server.start(cert, key)
    yield server
    server.stop()


@pytest.fixture(scope="module")
def disposable(site, tmp_path_factory):
    """A throwaway Chrome on the login page, registered in its own registry."""
    if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        pytest.skip("no plain Chrome binary for an isolated launch")
    root = tmp_path_factory.mktemp("login-browser")
    registry = str(root / "registry.json")
    instance = lifecycle.launch(
        headless=True,
        registry_path=registry,
        browser_args=["--ignore-certificate-errors", site.origin],
    )
    try:
        yield type("Disposable", (), {"registry": registry, "name": instance.name})()
    finally:
        lifecycle.stop(instance=instance.name, registry_path=registry)


@pytest.fixture
def fake_relay(tmp_path, monkeypatch):
    """The fake opchain first on PATH, plus an isolated config directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_opchain(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return config_home


def _wait_page_ready(origin: str, registry: str, name: str, *, navigate_url: str | None = None,
                    selector: str = "#go", timeout: float = 10.0) -> None:
    """Hold until the tab shows the expected origin and its form.

    Readiness is the observed page, never a sleep: the frame URL must be at
    the expected origin and the form's submit button must exist in its
    document. Without this, a login fired straight after Page.navigate (or
    straight after launch) races the document and fails with wrong-origin.
    """
    import asyncio

    from browser_tools.one_shot import one_shot_page_session

    async def go() -> None:
        port = lifecycle.resolve_cdp_port(name, registry_path=registry)
        async with one_shot_page_session(port, None, None) as (cdp, session_id):
            previous_loader = None
            if navigate_url is not None:
                before = await cdp.send(
                    method="Page.getFrameTree", params={},
                    session_id=session_id, timeout=5.0,
                )
                previous_loader = before["frameTree"]["frame"]["loaderId"]
                await cdp.send(
                    method="Page.navigate",
                    params={"url": navigate_url},
                    session_id=session_id,
                    timeout=10.0,
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ready = False
                try:
                    tree = await cdp.send(
                        method="Page.getFrameTree", params={},
                        session_id=session_id, timeout=5.0,
                    )
                    frame = tree["frameTree"]["frame"]
                    if previous_loader is None or frame.get("loaderId") != previous_loader:
                        check = await cdp.send(
                            method="Runtime.evaluate",
                            params={
                                "expression": f"document.location.origin === {json.dumps(origin)} && "
                                              f"Boolean(document.querySelector({selector!r}))",
                                "returnByValue": True,
                            },
                            session_id=session_id, timeout=5.0,
                        )
                        ready = check.get("result", {}).get("value") is True
                except Exception:
                    ready = False  # mid-navigation: the document is not ready
                if ready:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"the page at {origin} never became ready")

    asyncio.run(go())


def _navigate_to(site, disposable, path: str = "/") -> None:
    """Put the disposable browser's single tab back on a chosen page, ready."""
    _wait_page_ready(
        site.origin, disposable.registry, disposable.name,
        navigate_url=site.origin + path,
    )


def _run_login(disposable, policy: Path, monkeypatch, *, timeout_ms=20_000, target=None):
    from browser_tools import cli

    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", disposable.registry)
    args = ["login", disposable.name, "--account", "example",
            "--config", str(policy), "--timeout-ms", str(timeout_ms)]
    if target is not None:
        args.extend(["--target", target])
    return cli.main(args)


class TestRefusalDiagnostics:
    @pytest.mark.parametrize("phase", ["before-fill", "constraint-validation"])
    def test_refusal_does_not_claim_an_unperformed_action(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, phase
    ):
        import contextlib

        from browser_tools import login

        original_page = site.page
        real_session = login.one_shot_page_session
        real_read = login._read_credentials
        observed = {}

        @contextlib.asynccontextmanager
        async def inspect_session(*args):
            async with real_session(*args) as (cdp, session_id):
                async def read_then_disable(*read_args):
                    pair = await real_read(*read_args)
                    await cdp.send(
                        method="Runtime.evaluate", session_id=session_id,
                        params={"expression": "document.querySelector('#username').disabled = true"},
                    )
                    return pair

                if phase == "before-fill":
                    monkeypatch.setattr(login, "_read_credentials", read_then_disable)
                try:
                    yield cdp, session_id
                finally:
                    result = await cdp.send(
                        method="Runtime.evaluate", session_id=session_id,
                        params={
                            "expression": "document.querySelector('#username').value === '' && "
                                          "document.querySelector('#password').value === ''",
                            "returnByValue": True,
                        },
                    )
                    observed["empty"] = result["result"]["value"]

        try:
            site.page = LOGIN_PAGE
            if phase == "constraint-validation":
                site.page = LOGIN_PAGE.replace('name="u"', 'name="u" pattern="no-match"')
            _navigate_to(site, disposable)
            site.posts.clear()
            policy = _write_policy(tmp_path, site.origin)
            monkeypatch.setattr(login, "one_shot_page_session", inspect_session)
            assert _run_login(disposable, policy, monkeypatch) == 1
            output = capfd.readouterr()
            if phase == "before-fill":
                assert "disabled" in output.err
                assert "may still hold" not in output.err
            else:
                assert "validation" in output.err and "prevented submission" in output.err
            assert observed["empty"] is True
            assert not site.posts
            assert _relay_reads(fake_relay) == ["username", "password"]
            assert SENTINEL_USER not in output.out + output.err
            assert SENTINEL_PASS not in output.out + output.err
        finally:
            site.page = original_page


class TestTheWholeFlow:
    @pytest.mark.parametrize("case", ["several-tabs", "missing-target"])
    def test_target_selection_errors_never_print_browser_metadata(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, case
    ):
        import asyncio

        from browser_tools.one_shot import one_shot_page_session

        port = lifecycle.resolve_cdp_port(disposable.name, registry_path=disposable.registry)
        targets = {}

        async def create_extra():
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                current = await cdp.send("Target.getTargetInfo", {}, session_id=session_id)
                targets["original"] = current["targetInfo"]["targetId"]
                extra = await cdp.send(
                    "Target.createTarget", {"url": "about:blank#SENTINEL-browser-url"},
                )
                targets["extra"] = extra["targetId"]

        async def close_extra():
            async with one_shot_page_session(port, targets["original"], "id") as (cdp, _):
                await cdp.send("Target.closeTarget", {"targetId": targets["extra"]})

        try:
            if case == "several-tabs":
                asyncio.run(create_extra())
            site.posts.clear()
            policy = _write_policy(tmp_path, site.origin)
            target = "SENTINEL-missing-target" if case == "missing-target" else None
            assert _run_login(disposable, policy, monkeypatch, target=target) == 1
            captured = capfd.readouterr()
            assert "--target" in captured.err
            assert "SENTINEL" not in captured.out + captured.err
            assert "Traceback" not in captured.err
            assert captured.out == ""
            assert not _relay_reads(fake_relay)
            assert not site.posts
        finally:
            if "extra" in targets:
                asyncio.run(close_extra())

    @pytest.mark.parametrize("dialog", ["alert", "prompt"])
    def test_synthetic_dialog_is_dismissed_without_exposing_its_contents(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, dialog
    ):
        original_page = site.page
        expression = (
            "alert(event.target.value) === undefined" if dialog == "alert"
            else "prompt(document.querySelector('#username').value, event.target.value) === null"
        )
        try:
            site.page = LOGIN_PAGE + "<script>" + (
                "document.querySelector('#password').addEventListener('input', function(event) {"
                f"window.dialogDismissed = {expression};"
                "});"
                "document.querySelector('form').addEventListener('submit', function(event) {"
                "if (window.dialogDismissed !== true) event.preventDefault();"
                "});"
            ) + "</script>"
            _navigate_to(site, disposable)
            site.posts.clear()
            policy = _write_policy(tmp_path, site.origin)
            assert _run_login(disposable, policy, monkeypatch) == 0
            assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
            captured = capfd.readouterr()
            assert json.loads(captured.out)["status"] == "authenticated"
            assert SENTINEL_USER not in captured.out + captured.err
            assert SENTINEL_PASS not in captured.out + captured.err
        finally:
            site.page = original_page

    @pytest.fixture(autouse=True)
    def back_on_the_login_page(self, site, disposable):
        """Each test starts from the form; the last one navigated away."""
        _navigate_to(site, disposable)

    def test_a_valid_form_fills_submits_once_and_authenticates(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch) == 0
        captured = capfd.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}
        # Exactly one POST, carrying both synthetic values exactly once.
        assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
        assert "SENTINEL" not in captured.err

    def test_wrong_origin_refuses_without_submitting(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, "https://127.0.0.1:1")
        assert _run_login(disposable, policy, monkeypatch) == 1
        err = capfd.readouterr().err
        assert "configured origin" in err
        assert site.posts == []
        assert "SENTINEL" not in err

    def test_success_selector_already_matching_refuses(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin, success_selector="form")
        assert _run_login(disposable, policy, monkeypatch) == 1
        assert "already holds" in capfd.readouterr().err
        assert site.posts == []

    def test_two_preexisting_success_markers_refuse_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """Zero matches required, not exactly-one absence: a page already
        signed in with the marker repeated must refuse before the relay."""
        monkeypatch.setattr(site, "page", TWO_MARKERS_PAGE)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch) == 1
        captured = capfd.readouterr()
        assert "already holds" in captured.err
        assert _relay_reads(fake_relay) == []
        assert site.posts == []
        assert "SENTINEL" not in captured.out + captured.err

    def test_password_in_a_separate_form_refuses_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """Username and submit share one form, the password sits in another.

        Everything else about both forms is valid, so only the common-form
        check refuses; it must do so before any credential is read or sent.
        """
        monkeypatch.setattr(site, "page", SEPARATE_FORM_PAGE)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch) == 1
        captured = capfd.readouterr()
        assert "same connected form" in captured.err
        assert _relay_reads(fake_relay) == []
        assert site.posts == []
        assert "SENTINEL" not in captured.out + captured.err

    def test_ambiguous_username_selector_refuses(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin, username_selector="input")
        assert _run_login(disposable, policy, monkeypatch) == 1
        assert "exactly one" in capfd.readouterr().err
        assert site.posts == []

    @pytest.mark.parametrize("override", [False, True])
    def test_invalid_post_method_does_not_send_credentials_in_a_get(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, override
    ):
        if override:
            page = LOGIN_PAGE.replace('id="go"', 'id="go" formmethod=" post "')
        else:
            page = LOGIN_PAGE.replace('method="post"', 'method=" post "')
        monkeypatch.setattr(site, "page", page)
        _navigate_to(site, disposable)
        site.posts.clear()
        before = len(site.gets)
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
        assert "POST" in capfd.readouterr().err
        assert site.posts == []
        assert not any("?" in path for path in site.gets[before:])

    @pytest.mark.parametrize("change", ["hidden-user", "hidden-password", "hidden-submit", "disabled-fieldset"])
    def test_unusable_fields_are_refused(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, change
    ):
        page = LOGIN_PAGE
        if change == "disabled-fieldset":
            page = page.replace('<input id="username"', '<fieldset disabled><input id="username"').replace('</form>', '</fieldset></form>')
        else:
            identifier = {"hidden-user": "username", "hidden-password": "password", "hidden-submit": "go"}[change]
            page = page.replace(f'id="{identifier}"', f'id="{identifier}" style="display:none"')
        monkeypatch.setattr(site, "page", page)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
        assert "disabled" in capfd.readouterr().err
        assert site.posts == []


class TestClearingOnASinglePageForm:
    """The SPA shape: no navigation, so the live fields must be cleared."""

    @pytest.fixture(autouse=True)
    def fresh_spa(self, spa):
        spa.server.page = SPA_PAGE
        _navigate_to(spa.server, spa)

    @pytest.mark.parametrize("case", ["no-success", "replaced-fields"])
    def test_late_values_are_cleared_or_reported_on_every_exit(
        self, spa, fake_relay, tmp_path, monkeypatch, capfd, case
    ):
        original_page = spa.server.page
        page = SPA_PAGE
        if case == "no-success":
            page = page.replace("marker.setAttribute('data-signed-in', '');", "")
        else:
            form = "<form" + LOGIN_PAGE.split("<form", 1)[1].split("</form>", 1)[0] + "</form>"
            page = page.replace(
                "window.stateAfterResponse =",
                f"document.querySelector('form').outerHTML = {json.dumps(form)};"
                "window.stateAfterResponse =",
            )
        try:
            spa.server.page = page
            _navigate_to(spa.server, spa)
            spa.server.posts.clear()
            policy = _write_policy(tmp_path, spa.server.origin)
            assert _run_login(spa, policy, monkeypatch, timeout_ms=1000) == 1
            captured = capfd.readouterr()
            assert spa.server.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
            state = self._page_state(spa)
            if case == "no-success":
                assert state["u"] == "" and state["p"] == ""
                assert "success condition" in captured.err
            else:
                assert "may still hold" in captured.err
            assert SENTINEL_USER not in captured.out + captured.err
            assert SENTINEL_PASS not in captured.out + captured.err
        finally:
            spa.server.page = original_page

    @pytest.fixture(scope="class")
    def spa(self, tmp_path_factory):
        """A single-page form site plus a throwaway Chrome opened on it."""
        if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
            pytest.skip("no plain Chrome binary for an isolated launch")
        tmp = tmp_path_factory.mktemp("login-spa")
        cert, key = _make_cert(tmp)
        server = LoginFormServer(SPA_PAGE)
        server.start(cert, key)
        registry = str(tmp / "registry.json")
        instance = lifecycle.launch(
            headless=True,
            registry_path=registry,
            browser_args=["--ignore-certificate-errors", server.origin],
        )
        try:
            # The tab must actually show the form before any test logs in:
            # the launch navigation is asynchronous and races otherwise.
            _wait_page_ready(server.origin, registry, instance.name)
            yield type(
                "Spa", (), {"registry": registry, "name": instance.name, "server": server}
            )()
        finally:
            lifecycle.stop(instance=instance.name, registry_path=registry)
            server.stop()

    def _page_state(self, spa) -> dict[str, object]:
        import asyncio

        from browser_tools.one_shot import one_shot_page_session

        async def read() -> dict[str, object]:
            port = lifecycle.resolve_cdp_port(spa.name, registry_path=spa.registry)
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                result = await cdp.send(
                    method="Runtime.evaluate",
                    params={
                        "expression": "JSON.stringify({"
                                     "u: document.getElementById('username').value,"
                                     " p: document.getElementById('password').value,"
                                     " cleanAtYield: window.submitClearedBeforeYield === true,"
                                     " stateAfterResponse: window.stateAfterResponse || null})",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                return json.loads(result["result"]["value"])

        return asyncio.run(read())

    def test_fields_clear_without_clobbering_framework_state(
        self, spa, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """The SPA shape: an async handler POSTs from framework-like state.

        The fixture's state is maintained from the fill's input events, its
        handler awaits a real response before adding the success marker, and
        it re-renders both fields from state once the response lands. The
        login must: send the pair (from state, not the cleared DOM), clear
        both fields before the page's first microtask and before returning,
        keep the framework state intact while the handler is awaiting, and
        clear again after the response re-rendered the fields.
        """
        from browser_tools import cli

        spa.server.posts.clear()
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", spa.registry)
        policy = _write_policy(tmp_path, spa.server.origin)
        assert cli.main(
            ["login", spa.name, "--account", "example",
             "--config", str(policy), "--timeout-ms", "20000"]
        ) == 0
        captured = capfd.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}
        # The submission carried the synthetic pair, read from the page's
        # own framework-like state after an await, exactly once.
        assert spa.server.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
        # Fields were empty at the first microtask after submit...
        state = self._page_state(spa)
        assert state["cleanAtYield"] is True
        # ...the handler's state still held the pair when the response
        # landed (clearing did not overwrite it)...
        assert state["stateAfterResponse"] == {"u": SENTINEL_USER, "p": SENTINEL_PASS}
        # ...and the fields are empty again after the response's re-render:
        # the final cleanup confirmation cleared the restored values.
        assert state["u"] == "" and state["p"] == ""
        assert "SENTINEL" not in captured.err
