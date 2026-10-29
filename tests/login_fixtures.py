"""Reusable live-login fixtures: disposable Chrome, HTTPS site, fake relay.

Everything synthetic: a throwaway headless Chrome (never the developer's
browser, launched unnamed like any ordinary instance), a loopback HTTPS
server with a self-signed certificate serving hand-written login pages, and
a fake ``opchain`` on PATH that answers the relay probe and writes sentinel
credentials from a fixed table. No 1Password account, real credential, or
real site is involved.

Extracted from test_login_live.py (repository module-size rule): the
behavioral contract tests import these fixtures and helpers explicitly; no
star imports, no duplicate definitions.

Skipped when no plain Chrome binary (application-bundle launches abort under
repeated test launches) or no openssl for the certificate is available; the
gap is then reported by the worker rather than silently claimed.
"""

from __future__ import annotations

import asyncio
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
from browser_tools.one_shot import one_shot_page_session

__all__ = [
    "LOGIN_PAGE",
    "SENTINEL_PASS",
    "SENTINEL_USER",
    "SEPARATE_FORM_PAGE",
    "SPA_PAGE",
    "TWO_MARKERS_PAGE",
    "VAULT",
    "_make_cert",
    "_navigate_to",
    "_relay_reads",
    "_run_login",
    "_wait_page_ready",
    "_write_fake_opchain",
    "_write_policy",
    "disposable",
    "fake_relay",
    "site",
    "spa",
]


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


def _write_fake_opchain(bin_dir: Path, username: str = SENTINEL_USER) -> None:
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
        values = {{"username": {username!r}, "password": {SENTINEL_PASS!r}}}
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
    """A throwaway unnamed Chrome on the login page, in its own registry.

    Launched exactly as any ordinary ``bt launch`` - no named profile - so
    every live test proves ownership against a real recorded PID, start
    token and throwaway session directory (entry.profile is None), the same
    positive checks a profile-bound instance passes.
    """
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
        yield type(
            "Disposable", (), {"registry": registry, "name": instance.name}
        )()
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


@pytest.fixture(scope="class")
def spa(tmp_path_factory):
    """A single-page form site plus a throwaway unnamed Chrome opened on it."""
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
