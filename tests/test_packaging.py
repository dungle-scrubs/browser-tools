"""Packaging split: the default install is websockets-only and each heavy
dependency sits behind the extra RFC-01 assigns it (Question 4).

These assertions read ``pyproject.toml`` directly (the source of truth for the
dependency sets) and exercise the missing-extra guards in :mod:`browser_tools.
extras`, ``camoufox_runner``, and ``camoufox_session``. They need no network
and no optional dependency installed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from browser_tools.extras import (
    MissingExtraError,
    install_command,
    missing_extra_message,
    require_camoufox,
)

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _metadata() -> dict:
    return tomllib.loads(_PYPROJECT.read_text())


def _requirement_names(specs: list[str]) -> set[str]:
    """Reduce PEP 508 specifiers to their bare distribution names.

    ``camoufox[geoip]>=0.4.11`` -> ``camoufox``; ``websockets>=16.0`` ->
    ``websockets``. Extras and version markers are stripped so a test can assert
    which distributions a set pulls in without pinning exact versions.
    """
    names: set[str] = set()
    for spec in specs:
        head = spec.split(";", 1)[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(sep, 1)[0]
        names.add(head.strip().lower())
    return names


class TestDefaultDependencySet:
    """The base package (``pip install browser-tools``) is websockets-only."""

    def test_default_dependencies_are_websockets_only(self) -> None:
        deps = _metadata()["project"]["dependencies"]
        assert _requirement_names(deps) == {"websockets"}

    def test_websockets_floor_is_at_least_16(self) -> None:
        # RFC-01 "Packaging": the floor is >=16.0, the higher of the two merged
        # projects' floors (the vendored core requires it).
        deps = _metadata()["project"]["dependencies"]
        websockets = next(d for d in deps if d.lower().startswith("websockets"))
        assert ">=16.0" in websockets.replace(" ", "")

    def test_default_set_carries_no_camoufox_pillow_or_aiohttp(self) -> None:
        # Native is the default engine (post-#41): no camoufox, no pillow, and
        # the aiohttp CVE pin (forced only by camoufox) is gone from the default.
        deps = _requirement_names(_metadata()["project"]["dependencies"])
        assert "camoufox" not in deps
        assert "pillow" not in deps
        assert "aiohttp" not in deps


class TestExtras:
    """Each extra resolves to the dependencies RFC-01 Question 4 assigns it."""

    def _extras(self) -> dict[str, list[str]]:
        return _metadata()["project"]["optional-dependencies"]

    def test_camoufox_extra_carries_camoufox_and_the_cve_pin(self) -> None:
        names = _requirement_names(self._extras()["camoufox"])
        assert "camoufox" in names
        # The aiohttp >=3.14.1 CVE override travels with camoufox, which pulls
        # aiohttp in transitively; it is moot without camoufox installed.
        assert "aiohttp" in names

    def test_camoufox_extra_keeps_the_geoip_marker(self) -> None:
        specs = self._extras()["camoufox"]
        assert any("camoufox[geoip]" in s.replace(" ", "") for s in specs)

    def test_aiohttp_pin_floor_is_at_least_3_14_1(self) -> None:
        specs = self._extras()["camoufox"]
        aiohttp = next(s for s in specs if s.lower().startswith("aiohttp"))
        assert ">=3.14.1" in aiohttp.replace(" ", "")

    def test_profiling_extra_carries_pillow(self) -> None:
        assert _requirement_names(self._extras()["profiling"]) == {"pillow"}

    def test_all_extra_bundles_every_other_extra(self) -> None:
        extras = self._extras()
        all_names = _requirement_names(extras["all"])
        # `all` references the sibling extras by the project's own name.
        assert all_names == {"browser-tools"}
        combined = extras["all"][0].replace(" ", "")
        assert "camoufox" in combined
        assert "profiling" in combined


class TestMissingExtraMessage:
    """The failure text names the exact install line for the absent extra."""

    def test_install_command_is_the_exact_pip_line(self) -> None:
        assert install_command("camoufox") == "pip install 'browser-tools[camoufox]'"
        assert install_command("profiling") == "pip install 'browser-tools[profiling]'"

    def test_message_ends_with_the_install_command(self) -> None:
        msg = missing_extra_message("camoufox", "The Camoufox engine")
        assert msg.endswith("pip install 'browser-tools[camoufox]'")
        assert "'camoufox' extra" in msg

    def test_require_camoufox_raises_the_install_line_when_absent(self) -> None:
        with pytest.raises(MissingExtraError) as excinfo:
            require_camoufox(None)
        assert str(excinfo.value) == (
            "The Camoufox engine requires the 'camoufox' extra. "
            "Install it with: pip install 'browser-tools[camoufox]'"
        )

    def test_require_camoufox_is_a_noop_when_present(self) -> None:
        # Any non-None reference stands in for the imported Camoufox symbol.
        require_camoufox(object())


class TestCamoufoxEntryPointGuards:
    """The two Camoufox entry points fail with the install line, not a
    ``TypeError`` from calling the ``None`` fallback."""

    def test_runner_main_reports_the_install_line_without_the_extra(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        import builtins

        from browser_tools import camoufox_runner

        real_import = builtins.__import__

        def _no_camoufox(name: str, *args: object, **kwargs: object):
            if name == "camoufox.sync_api" or name.startswith("camoufox."):
                raise ImportError("camoufox extra not installed")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _no_camoufox)
        rc = camoufox_runner.main([f"--user-data-dir={tmp_path}"])
        assert rc == 3
        assert "pip install 'browser-tools[camoufox]'" in capsys.readouterr().err

    def test_session_launch_raises_the_install_line_without_the_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from browser_tools import camoufox_session

        # Simulate the extra being absent: the module-level import fell back to
        # None, which the launch guard turns into a MissingExtraError.
        monkeypatch.setattr(camoufox_session, "Camoufox", None)
        session = camoufox_session.CamoufoxSession()
        with pytest.raises(MissingExtraError) as excinfo:
            session._tool_launch_browser({})
        assert str(excinfo.value).endswith("pip install 'browser-tools[camoufox]'")
