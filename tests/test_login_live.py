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

# A single-page variant: the submit never navigates, so the fields must be
# cleared in the live document for the operation to claim success.
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
      document.getElementById('theform').addEventListener('submit', function (event) {
        event.preventDefault();
        queueMicrotask(function () {
          window.submitClearedBeforeYield =
            document.getElementById('username').value === '' &&
            document.getElementById('password').value === '';
        });
        setTimeout(function () {
          var marker = document.createElement('main');
          marker.setAttribute('data-signed-in', '');
          marker.textContent = 'Signed in';
          document.body.appendChild(marker);
        }, 50);
      });
    </script>
    """
)


class LoginFormServer:
    """The synthetic site: one form, one POST recorder, one welcome page."""

    def __init__(self, page: str, post_path: str) -> None:
        self.posts: list[dict[str, str]] = []
        self.gets: list[str] = []
        self.page = page
        self.post_path = post_path
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


def _write_fake_opchain(bin_dir: Path, origin: str) -> None:
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
        values = {{"username": {SENTINEL_USER!r}, "password": {SENTINEL_PASS!r}}}
        os.write(int(fd_text), values.get(field, "").encode("utf-8"))
        sys.exit(0)
        """
    ).lstrip("\n")
    path = bin_dir / "opchain"
    path.write_text(body)
    path.chmod(0o755)


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
    server = LoginFormServer(LOGIN_PAGE, "/login")
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
def fake_relay(tmp_path, monkeypatch, site):
    """The fake opchain first on PATH, plus an isolated config directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_opchain(bin_dir, site.origin)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return config_home


def _navigate_to(site, disposable, path: str = "/") -> None:
    """Put the disposable browser's single tab back on a chosen page."""
    import asyncio

    from browser_tools.one_shot import one_shot_page_session

    async def go() -> None:
        port = lifecycle.resolve_cdp_port(disposable.name, registry_path=disposable.registry)
        async with one_shot_page_session(port, None, None) as (cdp, session_id):
            await cdp.send(
                method="Page.navigate",
                params={"url": site.origin + path},
                session_id=session_id,
                timeout=10.0,
            )

    asyncio.run(go())


def _run_login(site, disposable, policy: Path, monkeypatch, *, timeout_ms=20_000):
    from browser_tools import cli

    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", disposable.registry)
    return cli.main(
        ["login", disposable.name, "--account", "example",
         "--config", str(policy), "--timeout-ms", str(timeout_ms)]
    )


class TestTheWholeFlow:
    @pytest.fixture(autouse=True)
    def back_on_the_login_page(self, site, disposable):
        """Each test starts from the form; the last one navigated away."""
        _navigate_to(site, disposable)

    def test_a_valid_form_fills_submits_once_and_authenticates(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capsys
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(site, disposable, policy, monkeypatch) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}
        # Exactly one POST, carrying both synthetic values exactly once.
        assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
        assert "SENTINEL" not in captured.err

    def test_wrong_origin_refuses_without_submitting(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capsys
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, "https://127.0.0.1:1")
        assert _run_login(site, disposable, policy, monkeypatch) == 1
        err = capsys.readouterr().err
        assert "configured origin" in err
        assert site.posts == []
        assert "SENTINEL" not in err

    def test_success_selector_already_matching_refuses(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capsys
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin, success_selector="form")
        assert _run_login(site, disposable, policy, monkeypatch) == 1
        assert "already holds" in capsys.readouterr().err
        assert site.posts == []

    def test_ambiguous_username_selector_refuses(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capsys
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin, username_selector="input")
        assert _run_login(site, disposable, policy, monkeypatch) == 1
        assert "exactly one" in capsys.readouterr().err
        assert site.posts == []

    @pytest.mark.parametrize("override", [False, True])
    def test_invalid_post_method_does_not_send_credentials_in_a_get(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capsys, override
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
        assert _run_login(site, disposable, policy, monkeypatch, timeout_ms=1000) == 1
        assert "POST" in capsys.readouterr().err
        assert site.posts == []
        assert not any("?" in path for path in site.gets[before:])

    @pytest.mark.parametrize("change", ["hidden-user", "hidden-password", "hidden-submit", "disabled-fieldset"])
    def test_unusable_fields_are_refused(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capsys, change
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
        assert _run_login(site, disposable, policy, monkeypatch, timeout_ms=1000) == 1
        assert "disabled" in capsys.readouterr().err
        assert site.posts == []


class TestClearingOnASinglePageForm:
    """The SPA shape: no navigation, so the live fields must be cleared."""

    @pytest.fixture(scope="class")
    def spa(self, tmp_path_factory):
        """A single-page form site plus a throwaway Chrome opened on it."""
        if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
            pytest.skip("no plain Chrome binary for an isolated launch")
        tmp = tmp_path_factory.mktemp("login-spa")
        cert, key = _make_cert(tmp)
        server = LoginFormServer(SPA_PAGE, "/spa-login")
        server.start(cert, key)
        registry = str(tmp / "registry.json")
        instance = lifecycle.launch(
            headless=True,
            registry_path=registry,
            browser_args=["--ignore-certificate-errors", server.origin],
        )
        try:
            yield type(
                "Spa", (), {"registry": registry, "name": instance.name, "server": server}
            )()
        finally:
            lifecycle.stop(instance=instance.name, registry_path=registry)
            server.stop()

    def _field_values(self, spa) -> dict[str, str | bool]:
        import asyncio

        from browser_tools.one_shot import one_shot_page_session

        async def read() -> dict[str, str | bool]:
            port = lifecycle.resolve_cdp_port(spa.name, registry_path=spa.registry)
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                result = await cdp.send(
                    method="Runtime.evaluate",
                    params={
                        "expression": "JSON.stringify({u: document.getElementById('username').value,"
                                      " p: document.getElementById('password').value,"
                                      " cleanAtYield: window.submitClearedBeforeYield})",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                return json.loads(result["result"]["value"])

        return asyncio.run(read())

    def test_fields_are_cleared_in_the_live_document(
        self, spa, fake_relay, tmp_path, monkeypatch, capsys
    ):
        from browser_tools import cli

        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", spa.registry)
        policy = _write_policy(tmp_path, spa.server.origin)
        assert cli.main(
            ["login", spa.name, "--account", "example",
             "--config", str(policy), "--timeout-ms", "20000"]
        ) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}
        # The submit never navigated, so clearing happened in the live DOM.
        assert self._field_values(spa) == {"u": "", "p": "", "cleanAtYield": True}
        # The page's own handler preventDefault()ed: the values it read were
        # the submitted pair, and nothing reached the wire from this form.
        assert spa.server.posts == []
