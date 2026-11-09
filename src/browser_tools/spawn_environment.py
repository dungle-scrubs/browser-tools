"""The environment a process browser-tools detaches must not inherit.

Herdr's terminal core gives every terminal identity variables
(``HC_TERMINAL_ID``, ``HC_SCOPE``, ``HC_TERMINAL_SOCKET`` and the ``HERDR_*``
family). When a terminal ends it sends ``SIGHUP``, then ``SIGTERM``, then
kills every process it can read whose environment carries that terminal's
identity, including processes that already left the terminal's session with
``start_new_session=True``. browser-tools passed its whole environment to the
processes it detaches, so a supervisor launched from a Herdr terminal died with
that terminal while its browser ran on unsupervised (#213).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Prefixes of the terminal-identity variables a detached process must not
#: carry, because the terminal that launched it ends every process holding
#: them.
TERMINAL_IDENTITY_PREFIXES: tuple[str, ...] = ("HC_", "HERDR_")


def detached_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``environ`` without the launching terminal's identity.

    The copy is what a long-lived detached process is spawned with. The
    terminal-identity variables are removed because the terminal that owns them
    ends every process carrying them, whether or not that process has left the
    terminal's session (#213). Nothing else is touched: every other key keeps
    its name and its value, and the mapping passed in is never modified.

    Args:
        environ: The environment the detached process would otherwise inherit.

    Returns:
        A new dictionary holding every entry of ``environ`` whose key does not
        start with one of :data:`TERMINAL_IDENTITY_PREFIXES`.
    """
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(TERMINAL_IDENTITY_PREFIXES)
    }
