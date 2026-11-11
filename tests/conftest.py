"""Pytest configuration and fixtures for browser-tools tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from launcher_spawn_helpers import stub_spawn_environment

from browser_tools import lifecycle

if TYPE_CHECKING:
    from collections.abc import Iterator


#: Set for the whole session so no test can launch the developer's own browser.
#: Auto-detection finds ``/Applications/Google Chrome.app``, and executing an
#: application bundle's inner binary directly aborts in macOS application
#: registration under repeated launches. A suite that does it kills their Chrome
#: over and over and raises a system crash dialog each time. A headless shell is
#: a plain binary, registers as nothing, and never aborts.
def _plain_chrome_binary() -> str | None:
    """A Chrome binary that is not an application bundle, or None."""
    roots: list[Path] = []
    named = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if named:
        roots.append(Path(named))
    roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        if not root.is_dir():
            continue
        found = sorted(root.glob("chromium_headless_shell-*/*/chrome-headless-shell"))
        for candidate in reversed(found):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def pytest_configure(config):
    """Pin the Chrome binary before any fixture, including module-scoped ones.

    This runs once per session rather than as an autouse fixture, because
    ``curated_browser`` is module-scoped and would launch before a
    function-scoped fixture could set anything. An operator who has already
    chosen a binary keeps it.
    """
    if os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        return
    binary = _plain_chrome_binary()
    if binary is not None:
        os.environ[lifecycle.CHROME_BINARY_ENV_VAR] = binary


def pytest_collection_modifyitems(config, items):
    """Auto-configure asyncio mode for all async tests."""
    for item in items:
        if item.get_closest_marker("asyncio") is not None:
            item.add_marker(pytest.mark.asyncio(loop_scope="function"))


@pytest.fixture(autouse=True)
def isolated_profile_roots(tmp_path_factory, monkeypatch):
    """Point both profile roots at empty temp dirs for every test.

    The legacy root defaults to ``/tmp/browser-tools-profiles``, which on a
    developer machine holds real logged-in profiles. Code under test reads,
    migrates and deletes profile directories, so no test may be one typo away
    from the real ones. A test that wants either root sets it itself; this
    fixture only guarantees the default is never the machine's own.
    """
    root = tmp_path_factory.mktemp("profile-roots")
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root / "profiles"))
    monkeypatch.setenv(lifecycle.LEGACY_PROFILES_ENV_VAR, str(root / "legacy"))


@pytest.fixture(scope="session", autouse=True)
def isolated_official_onepassword_source(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Point the official 1Password source at one absent synthetic path.

    The profile roots are isolated per test, but the automatic paths' source
    check reads a fixed path in the developer's own ``~/Library``: on a
    machine with the desktop app installed, a general unit test that launches
    a fresh Chrome session would otherwise read the real registration and run
    a real codesign against it. The isolation is session-scoped so it is in
    force before any module-scoped fixture runs (``curated_browser`` is
    module-scoped and set up before any function-scoped patch could apply).
    Every test therefore starts with the source absent; the positive fixtures
    (``valid_installation`` and the tests that plant their own source)
    override it with a synthetic one for their own scope, and pytest's
    monkeypatch nesting restores this absent session source when each
    override is torn down.
    """
    from browser_tools import onepassword

    absent = (
        tmp_path_factory.mktemp("absent-official-source")
        / onepassword.HOSTS_DIR_NAME
        / f"{onepassword.HOST_NAME}.json"
    )
    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(onepassword, "SOURCE_MANIFEST", absent)
        yield


@pytest.fixture(scope="module")
def official_source_at_module_scope() -> Path:
    """Capture the official source path before any function-scoped fixture.

    Module-scoped fixtures (``curated_browser`` among them) are set up before
    the function-scoped fixtures of their first test, so what this captures
    is exactly what a module-scoped browser launch would read. The witness
    test asserts the session-level isolation above was already in force at
    that point; the fixture itself never touches the real source.
    """
    from browser_tools import onepassword

    return onepassword.source_manifest_path()


#: The port a person's own Chrome is most likely to be on, and the port every
#: DevTools default names. A test that reaches it drives the developer's real
#: browser: it opens an unauthenticated CDP session to whatever they had open
#: and can read their tabs. It happened once, from a test that passed
#: ``--endpoint http://127.0.0.1:9222`` while relying on a transport double to
#: intercept it; a later change moved the connection and the double stopped
#: intercepting, so the invocation went out to the real browser. The double was
#: the whole protection, and a double is not a boundary.
FORBIDDEN_PORT = 9222

#: The suite's own launches must not land on ``FORBIDDEN_PORT``.
#:
#: ``core.registry.allocate_port`` probes upward from ``BASE_PORT``, which is
#: 9222, so the first browser the suite launches takes 9222 whenever nothing
#: else holds it. The guard below then refuses the fixture's own endpoint and
#: 25 tests fail. That is exactly what happened: while a developer's instance
#: held 9222 the suite silently got 9223 and up, and the day that instance went
#: away the suite started failing on a tree that had not changed.
#:
#: Moving the base up is the whole fix. It keeps ``allocate_port``'s probing
#: and its collision handling, costs the suite nothing, and leaves 9222
#: meaning one thing only: a browser this suite did not launch.
TEST_BASE_PORT = 9422


@pytest.fixture(scope="session", autouse=True)
def _suite_ports_start_above_the_default_port():
    """Move the suite's port allocation off ``FORBIDDEN_PORT`` for the session."""
    from browser_tools.core import registry

    original_base = registry.BASE_PORT
    original_max = registry.MAX_PORT
    registry.BASE_PORT = TEST_BASE_PORT
    registry.MAX_PORT = TEST_BASE_PORT + 100
    try:
        yield
    finally:
        registry.BASE_PORT = original_base
        registry.MAX_PORT = original_max


@pytest.fixture(autouse=True)
def no_test_talks_devtools_on_the_default_port(monkeypatch):
    """Fail any test that opens a DevTools conversation with ``FORBIDDEN_PORT``.

    Two chokepoints, because every path in the tree ends at one of them:
    ``urllib.request.urlopen`` for the ``/json*`` reads, vendored and not, and
    ``CDPClient.__init__`` for the WebSocket, which ``ExternalClient``
    inherits. Guarding the URL rather than the function name means a route
    nobody thought of is still stopped.

    Not guarded: a bare TCP connect. ``core.registry.allocate_port`` probes
    from 9222 upward to find a free port, which is one connect and close and
    is how ``bt launch`` picks its port at all. Refusing that would refuse the
    suite's whole launch path to prevent nothing.
    """
    import urllib.request

    from browser_tools.core.cdp_client import CDPClient

    real_urlopen = urllib.request.urlopen
    real_init = CDPClient.__init__

    def refuse(where):
        raise AssertionError(
            f"a test spoke DevTools to port {FORBIDDEN_PORT} ({where}). That is the "
            "default DevTools port and, on a developer machine, a browser they are "
            "using. Bind a port with socket() and pass that number instead."
        )

    def names_the_port(url):
        return f":{FORBIDDEN_PORT}/" in url or url.endswith(f":{FORBIDDEN_PORT}")

    def guarded_urlopen(request, *args, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        if names_the_port(url):
            refuse(url)
        return real_urlopen(request, *args, **kwargs)

    def guarded_init(self, ws_url, *args, **kwargs):
        if isinstance(ws_url, str) and names_the_port(ws_url):
            refuse(ws_url)
        return real_init(self, ws_url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)
    monkeypatch.setattr(CDPClient, "__init__", guarded_init)


@pytest.fixture
def isolated_default_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point BOTH default-registry aliases at one synthetic path.

    The session root is shared by every registry, so the real orphan sweep
    honors the default registry's instances even when the caller passed an
    isolated registry path. A real-sweep test that leaves an alias at the
    developer's own ``/tmp/chrome-agent/registry.json`` reads the
    developer's registry: ``launcher.REGISTRY_PATH`` (the imported alias the
    sweep compares and falls back against) and ``core.registry.REGISTRY_PATH``
    (the module-global fallback a None path resolves through) both need the
    synthetic path (round-4 tests F1).
    """
    from browser_tools.core import launcher as core_launcher
    from browser_tools.core import registry as core_registry

    synthetic = tmp_path / "synthetic-default-registry.json"
    monkeypatch.setattr(core_launcher, "REGISTRY_PATH", str(synthetic))
    monkeypatch.setattr(core_registry, "REGISTRY_PATH", str(synthetic))
    return synthetic


@pytest.fixture
def synthetic_spawn_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stub the OS-environment copy boundary to one synthetic mapping.

    A spawn double can then reject any environment that is not this mapping
    without ever seeing a real one. ``stub_spawn_environment`` explains why.
    """
    return stub_spawn_environment(monkeypatch)


@pytest.fixture
def sample_tool_response():
    """Sample tool response."""
    return {"content": [{"type": "text", "text": "Operation completed successfully"}]}


@pytest.fixture
def sample_error_response():
    """Sample error response."""
    return {"error": {"code": -32600, "message": "Element not found"}}


@pytest.fixture(scope="module")
def curated_browser(tmp_path_factory):
    """An isolated Chrome for the RFC-05 acceptance tests; never the user's tabs."""
    if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        pytest.skip(
            "no Chrome outside an application bundle was found, and launching "
            "the one in /Applications crashes the developer's browser; run "
            "`python -m playwright install chromium`"
        )
    root = tmp_path_factory.mktemp("curated-browser")
    registry = str(root / "registry.json")
    try:
        # ``about:blank`` because a headless shell starts with no tab at all,
        # where the full browser opens one. Without it the launch succeeds and
        # every verb then fails with "Browser is running but has no open pages".
        instance = lifecycle.launch(
            headless=True, registry_path=registry, browser_args=["about:blank"]
        )
    except lifecycle.LifecycleError as exc:
        pytest.skip(f"cannot launch isolated Chrome: {exc}")
    try:
        yield f"http://127.0.0.1:{instance.port}"
    finally:
        lifecycle.stop(instance=instance.name, registry_path=registry)
