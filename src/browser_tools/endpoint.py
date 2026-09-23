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

The rule runs twice, because the HTTP form asks the peer for an address. A
listener on ``127.0.0.1`` that answers ``/json/version`` with
``{"webSocketDebuggerUrl": "ws://192.0.2.1:41234/devtools/browser/x"}`` was
enough to make ``bt`` dial off-box, which made "loopback only" false on the
form the guide presents first. :func:`validate_peer_websocket_url` re-runs
:func:`resolve_endpoint_port` on whatever the peer answered, so the peer picks
nothing about where ``bt`` connects.

``Browser.close`` and ``Browser.crash`` are refused
---------------------------------------------------
Everything else passes, including navigation, input, cookies, and
``Target.closeTarget`` for a single tab. For a browser the tool launched,
quitting is what ``stop`` is for and the loss is a throwaway session. For the
user's real browser it is every window and tab they had open, reachable through
one typo in a verb that accepts arbitrary method names.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class ResolvedEndpoint:
    """An external address, never an Instance or a registry record.

    ``websocket_url`` is the one browser WebSocket this endpoint dials, or
    ``None`` when the address is the HTTP form and the URL is still to be read
    from ``/json/version``. It is one URL and not a list: a second connection
    is a second unauthenticated CDP session and, on a Chrome that asks for
    approval, a second Allow prompt for one invocation.

    ``host`` is the validated loopback host, kept in the bracketed form a URL
    uses (``127.0.0.1`` or ``[::1]``), so every later request dials the address
    that was checked instead of re-deriving one.

    ``flag`` is the flag the person typed. Diagnostics name it, because
    ``--endpoint`` and ``--chrome-profile`` reach the same code and a message
    naming the wrong one sends the reader to the wrong remedy.
    """

    port: int
    url: str
    websocket_url: str | None = None
    host: str = "127.0.0.1"
    flag: str = "--endpoint"


def for_url(host: str) -> str:
    """Bracket an IPv6 literal so it can sit in a URL authority."""
    return f"[{host}]" if ":" in host else host


def validate_peer_websocket_url(url: object, *, source: str) -> str:
    """Re-check a WebSocket URL a peer chose, with the flag's own validator.

    ``/json/version`` is answered by whatever holds the port, and its
    ``webSocketDebuggerUrl`` names a host. Handing that host to the WebSocket
    client lets the peer pick the address ``bt`` dials, which is the loopback
    rule undone by the thing the rule exists to contain. The same validator the
    flag runs therefore runs again here, on the answer.

    Raises:
        ConnectionError: The peer answered with something that is not a
            loopback browser WebSocket URL. This is the peer's failure, not the
            caller's typing, so it is exit 1 with a diagnostic rather than a
            usage error.
    """
    if not isinstance(url, str) or not url:
        raise ConnectionError(
            f"{source} answered /json/version without a webSocketDebuggerUrl, "
            "so it is not a DevTools HTTP endpoint."
        )
    try:
        resolved = resolve_endpoint_port(url)
    except EndpointUsageError as exc:
        raise ConnectionError(
            f"{source} answered /json/version with webSocketDebuggerUrl {url!r}, and "
            f"bt will not dial it. The peer on the port does not get to choose the "
            f"address bt connects to. {exc}"
        ) from exc
    if resolved.websocket_url is None:
        raise ConnectionError(
            f"{source} answered /json/version with webSocketDebuggerUrl {url!r}, "
            "which is not a ws:// browser WebSocket URL."
        )
    return resolved.websocket_url


def resolve_endpoint_port(endpoint: str) -> ResolvedEndpoint:
    """Validate the entire address before any connection is attempted."""
    text = endpoint.strip()
    if "//" not in text:
        text = f"http://{text}"
    try:
        parsed = urlparse(text)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EndpointUsageError(f"invalid --endpoint {endpoint!r}: {exc}") from exc
    if parsed.scheme not in ("http", "https", "ws"):
        raise EndpointUsageError("--endpoint requires an http(s) URL or ws://HOST:PORT/devtools/browser/ID")
    if not host:
        raise EndpointUsageError(f"--endpoint {endpoint!r} names no host")
    if port is None:
        raise EndpointUsageError(f"--endpoint {endpoint!r} names no port")
    if not port:
        raise EndpointUsageError("--endpoint port must be between 1 and 65535")
    if host not in LOOPBACK_HOSTS:
        raise EndpointUsageError(
            f"--endpoint {endpoint!r} is not loopback. A CDP endpoint is "
            "unauthenticated full control of a logged-in browser, so only "
            f"{' and '.join(sorted(LOOPBACK_HOSTS))} are accepted. "
            + _TUNNEL_REMEDY.format(port=port)
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EndpointUsageError("--endpoint cannot contain credentials, a query, or a fragment")
    if parsed.scheme == "ws":
        prefix = "/devtools/browser/"
        if not parsed.path.startswith(prefix) or not parsed.path[len(prefix):] or any(
            c.isspace() for c in text
        ):
            raise EndpointUsageError("--endpoint ws URL requires /devtools/browser/ID; HTTP discovery uses http://HOST:PORT")
        return ResolvedEndpoint(port, text, text, for_url(host))
    return ResolvedEndpoint(port, text, None, for_url(host))


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
