"""External CDP endpoints: `--endpoint URL` (RFC-01 v6, "External endpoints").

A person logs into a browser they already trust, and the agent drives that
browser. ``--endpoint URL`` is a flag on the browser-driving verbs, per
invocation.

**Nothing is written to the registry, and that absence is the safety property.**
An external Chrome is the user's real browser, so its user-data-dir is the real
Chrome profile directory. Two vendored paths ``rmtree`` whatever that field
holds: the retirement path the supervisor calls on close, and ``cleanup`` for
any entry it judges dead. A guard in the lifecycle layer would not hold, because
the vendored ``cleanup`` iterates the registry itself and a guard above it can
be bypassed. With no entry, ``stop`` and ``cleanup`` cannot see the browser, so
there is nothing to guard.

That settles the rest for free. No new verb, and no collision with ``attach``,
which keeps meaning "stream subscribed CDP events". No supervisor, so no window
marking and ``window-border`` does not reach it. A browser that dies mid-session
surfaces as a connection error on the next invocation, through the one-shot
error handling every verb already has.

Two refusals live here; the third is the CLI's, because it is about which verbs
accept the flag at all.

Loopback only, with no escape hatch
-----------------------------------
A CDP endpoint is unauthenticated full control of a logged-in browser, cookies
included, so a non-loopback endpoint is a remote takeover channel. Only
``127.0.0.1`` and ``::1`` are accepted -- not ``localhost``, which resolves to
either and sometimes to neither. ``ssh -L 9222:127.0.0.1:9222 <host>`` presents
a remote endpoint as loopback locally, so the safe pattern is the only pattern.
The retired attach handler carried a ``BROWSER_TOOLS_ALLOW_REMOTE_ENDPOINT``
escape hatch; it does not come back.

``Browser.close`` and ``Browser.crash`` are refused
---------------------------------------------------
Everything else passes, including navigation, input, cookies, and
``Target.closeTarget`` for a single tab. For a browser the tool launched,
quitting is what ``stop`` is for and the loss is a throwaway session. For the
user's real browser it is every window and tab they had open, reachable through
one typo in a verb that accepts arbitrary method names.
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import process_utils
from .usage import UsageError

#: The only hosts an endpoint may name. ``localhost`` is deliberately absent:
#: it resolves to either of these and sometimes to neither, and the remedy is
#: one character of typing.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

#: Methods refused over an external endpoint. Both end the user's whole browser.
BROWSER_LIFETIME_METHODS = frozenset({"Browser.close", "Browser.crash"})

#: What to tell someone whose endpoint is not loopback.
_TUNNEL_REMEDY = (
    "forward it instead: ssh -L {port}:127.0.0.1:{port} <host>, "
    "then use --endpoint http://127.0.0.1:{port}"
)


class EndpointUsageError(UsageError):
    """A malformed or refused ``--endpoint`` invocation (CLI exit code 2)."""


def resolve_endpoint_port(endpoint: str) -> int:
    """Validate an external endpoint and return the port to drive.

    Args:
        endpoint: The ``--endpoint`` value, e.g. ``http://127.0.0.1:9222``.

    Returns:
        The TCP port of the remote debugging endpoint.

    Raises:
        EndpointUsageError: The URL is malformed, names a non-loopback host, or
            carries no port. Nothing is sent anywhere first.
    """
    text = endpoint.strip()
    if "//" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)

    if parsed.scheme not in ("http", "https"):
        raise EndpointUsageError(
            f"--endpoint {endpoint!r} is not an http(s) URL; "
            "use http://127.0.0.1:<port>"
        )

    host = (parsed.hostname or "").strip("[]")
    if not host:
        raise EndpointUsageError(
            f"--endpoint {endpoint!r} names no host; use http://127.0.0.1:<port>"
        )

    port = parsed.port
    if port is None:
        raise EndpointUsageError(
            f"--endpoint {endpoint!r} names no port; use http://127.0.0.1:<port>"
        )

    if host not in LOOPBACK_HOSTS:
        raise EndpointUsageError(
            f"--endpoint {endpoint!r} is not loopback. A CDP endpoint is "
            "unauthenticated full control of a logged-in browser, so only "
            f"{' and '.join(sorted(LOOPBACK_HOSTS))} are accepted. "
            + _TUNNEL_REMEDY.format(port=port)
        )

    return port


def refuse_browser_lifetime_method(method: str) -> None:
    """Refuse a method that would end the user's whole browser.

    Raises:
        EndpointUsageError: ``method`` is ``Browser.close`` or ``Browser.crash``.
    """
    if method in BROWSER_LIFETIME_METHODS:
        raise EndpointUsageError(
            f"{method} is refused over --endpoint: it would close every window "
            "and tab of a browser this tool does not own. To close one tab: "
            "Target.closeTarget '{\"targetId\": \"<id>\"}' --target SPEC. "
            "To quit the browser, quit it yourself."
        )


def describe_endpoint(port: int) -> str:
    """Diagnose an endpoint that would not answer, for an error message.

    Reports who holds the port and what user-data-dir they hold, so a failed
    connection says which browser is actually there rather than only that the
    connection failed. Carried forward from the retired attach handler, which
    was the only place this diagnosis existed.

    Args:
        port: The endpoint's TCP port.

    Returns:
        A multi-line description, or a line saying nothing is listening.
    """
    listeners = process_utils.find_listeners_on_port(port)
    if not listeners:
        return f"nothing is listening on port {port}."

    lines = [f"port {port} is held by {len(listeners)} process(es):"]
    for pid in listeners:
        user_data_dir = process_utils.find_chrome_user_data_dir(pid)
        debug_port = process_utils.find_chrome_debug_port(pid)
        command = process_utils.read_process_command(pid) or "(command unreadable)"
        lines.append(
            f"  pid {pid}"
            f"  user-data-dir={user_data_dir if user_data_dir else '(none found)'}"
            f"  --remote-debugging-port={debug_port if debug_port else '(none found)'}"
        )
        lines.append(f"    {command[:200]}")
    if len(listeners) > 1:
        lines.append(
            "  more than one listener on this port is a collision; "
            "the one that answers may not be the one you mean."
        )
    return "\n".join(lines)
