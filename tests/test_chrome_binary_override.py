"""The Chrome binary override must win, including over ``--channel``.

This file exists because the override shipped as a fallback. The call site
read ``resolve_channel_binary(channel) or chrome_binary_from_env()``, so every
launch that named a channel resolved to the installed application bundle and
never consulted the variable. The test suite sets the variable to a headless
shell precisely so no test can launch an application bundle, and that
protection did nothing for any channel-naming caller. macOS aborts an
application bundle inside ``_RegisterApplication`` under repeated launches and
puts a crash dialog on screen for each one.

Nothing covered the precedence, which is why it shipped.
"""

from __future__ import annotations

import pytest

from browser_tools import lifecycle


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


@pytest.fixture
def captured_launch(monkeypatch, tmp_path):
    """Capture the kwargs the vendored launcher would receive."""
    seen: dict = {}

    async def fake_launch_browser(**kwargs):
        seen.update(kwargs)
        from browser_tools.core import registry as core_registry

        core_registry.register(
            working_dir=str(tmp_path / "site"),
            pid=4321,
            browser_version="Chrome/9",
            user_data_dir=str(tmp_path / "udd"),
            port_override=9250,
            registry_path=kwargs["registry_path"],
            pid_start="tok",
        )
        return core_registry.lookup("site-01", registry_path=kwargs["registry_path"])

    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", fake_launch_browser)
    return seen


class TestTheOverrideWins:
    def test_the_environment_beats_an_explicit_channel(
        self, captured_launch, monkeypatch, tmp_path, registry_path
    ):
        """A named channel must not defeat the override.

        This is the exact shape that reached a developer's screen: a caller
        names a channel, the channel resolves to the application bundle, and
        the override is never consulted.
        """
        shell = tmp_path / "chrome-headless-shell"
        shell.write_text("#!/bin/sh\n")
        shell.chmod(0o755)
        monkeypatch.setenv(lifecycle.CHROME_BINARY_ENV_VAR, str(shell))
        monkeypatch.setattr(
            lifecycle,
            "resolve_channel_binary",
            lambda channel: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )

        lifecycle.launch(channel="stable", headless=True, registry_path=registry_path)

        assert captured_launch["binary"] == str(shell)
        assert ".app/" not in captured_launch["binary"]

    def test_the_override_short_circuits_channel_resolution(
        self, captured_launch, monkeypatch, tmp_path, registry_path
    ):
        """A channel that would raise must not be resolved at all.

        ``resolve_channel_binary`` raises when a named channel is not
        installed. With the override set, that lookup never runs, so an
        override plus an absent channel is a working launch rather than an
        error about the channel.
        """
        shell = tmp_path / "chrome-headless-shell"
        shell.write_text("#!/bin/sh\n")
        shell.chmod(0o755)
        monkeypatch.setenv(lifecycle.CHROME_BINARY_ENV_VAR, str(shell))

        def explode(channel):
            raise AssertionError("channel resolution ran despite the override")

        monkeypatch.setattr(lifecycle, "resolve_channel_binary", explode)

        lifecycle.launch(channel="canary", headless=True, registry_path=registry_path)

        assert captured_launch["binary"] == str(shell)

    def test_the_channel_still_decides_when_no_override_is_set(
        self, captured_launch, monkeypatch, registry_path
    ):
        """Without the variable, ``--channel`` keeps its meaning."""
        monkeypatch.delenv(lifecycle.CHROME_BINARY_ENV_VAR, raising=False)
        monkeypatch.setattr(lifecycle, "resolve_channel_binary", lambda channel: "/bin/chrome-beta")

        lifecycle.launch(channel="beta", headless=True, registry_path=registry_path)

        assert captured_launch["binary"] == "/bin/chrome-beta"

    def test_a_bad_override_is_an_error_rather_than_a_silent_fallback(
        self, monkeypatch, tmp_path
    ):
        """Pointing the variable at a non-executable must not auto-detect."""
        monkeypatch.setenv(lifecycle.CHROME_BINARY_ENV_VAR, str(tmp_path / "nope"))
        with pytest.raises(lifecycle.LifecycleError) as exc:
            lifecycle.chrome_binary_from_env()
        assert lifecycle.CHROME_BINARY_ENV_VAR in str(exc.value)
