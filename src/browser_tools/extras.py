"""Optional-dependency ('extra') gating.

The default install (``pip install browser-tools``) depends on ``websockets``
only (RFC-01, "Packaging"). Features that need a heavier dependency live behind
an extra:

- ``camoufox`` -- the Camoufox anti-detect engine and its ``aiohttp`` CVE pin;
- ``profiling`` -- ``pillow`` (sharper screenshot blank-frame detection);
- ``all`` -- every extra at once.

A command that needs an absent extra MUST fail with the exact ``pip install``
line naming that extra. This module is the single source of that line and of
the guard that raises it, so the wording cannot drift between call sites.
"""

from __future__ import annotations


class MissingExtraError(RuntimeError):
    """A feature was invoked without its optional-dependency extra installed.

    The message is the exact install line for the extra, so a caller can print
    ``str(err)`` and the user gets a command they can run verbatim.
    """


def install_command(extra: str) -> str:
    """Return the exact command that installs the named extra.

    Args:
        extra: Extra name as declared in ``pyproject.toml`` (e.g. ``camoufox``).

    Returns:
        The literal ``pip install 'browser-tools[<extra>]'`` line.
    """
    return f"pip install 'browser-tools[{extra}]'"


def missing_extra_message(extra: str, feature: str) -> str:
    """Build the failure message for a feature whose extra is not installed.

    Args:
        extra: Extra name as declared in ``pyproject.toml``.
        feature: Human-readable name of the feature that needs the extra.

    Returns:
        A message ending in the exact :func:`install_command` line.
    """
    return f"{feature} requires the '{extra}' extra. Install it with: {install_command(extra)}"


def require_camoufox(camoufox: object, feature: str = "The Camoufox engine") -> None:
    """Fail with the camoufox install line when the extra is not installed.

    ``camoufox_session`` and ``camoufox_runner`` import ``camoufox.sync_api``
    inside a ``try`` and fall back to ``None`` when the extra is absent. Pass
    that (possibly ``None``) reference here before using it so the caller gets
    the exact install line instead of a ``TypeError`` on a ``None`` call.

    Args:
        camoufox: The imported Camoufox symbol, or ``None`` when unavailable.
        feature: Human-readable name used in the message.

    Raises:
        MissingExtraError: When ``camoufox`` is ``None``.
    """
    if camoufox is None:
        raise MissingExtraError(missing_extra_message("camoufox", feature))
