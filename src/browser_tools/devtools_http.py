"""DevTools HTTP reads that dial the address that was validated.

The vendored ``core.cdp_client`` and ``core.protocol`` request
``http://localhost:{port}/...``. ``localhost`` resolves to ``127.0.0.1`` or
``[::1]`` depending on the resolver's order, so a listener bound to one family
is reached through a flag that named the other, and a listener bound to the
family the resolver happens to prefer is reached through a flag that named
neither. Measured: a server bound to ``[::1]:54075`` only, reached by
``--endpoint http://127.0.0.1:54075``.

Every read here takes the host as an argument and puts it in the URL verbatim,
so ``--endpoint`` reaches the address the person typed and discovery reaches
the family it is probing. Call-site adaptation of the verbatim core (RFC-01,
"Vendoring rules"): ``core/`` is unchanged.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from .endpoint import ResolvedEndpoint, validate_peer_websocket_url

#: Seconds to wait for one DevTools HTTP read. Chrome serves ``/json*`` from
#: its UI thread, so a browser whose UI thread is wedged accepts the TCP
#: connection and never answers; without a bound the read waits forever. Two
#: seconds is the vendored client's own number, kept so both paths time out
#: alike.
HTTP_TIMEOUT = 2.0

#: ``/json/protocol`` is a much larger document than ``/json/version`` and is
#: read once, for ``help``. Five seconds is the vendored schema fetch's number.
SCHEMA_TIMEOUT = 5.0


def fetch_devtools_json(
    host: str, port: int, path: str, *, timeout: float = HTTP_TIMEOUT
) -> Any:
    """GET ``http://{host}:{port}{path}`` and parse the JSON body.

    Args:
        host: A loopback host in URL form -- ``127.0.0.1`` or ``[::1]``.
        port: The endpoint's TCP port.
        path: The DevTools HTTP path, beginning with a slash.
        timeout: Seconds to wait for the whole read.

    Returns:
        The parsed JSON body.

    Raises:
        OSError: The read failed. ``urllib``'s ``URLError`` and ``HTTPError``
            are both ``OSError``, so one class covers refused, unreachable,
            timed out, and a non-200 status.
        ValueError: The body was not JSON.
    """
    request = urllib.request.Request(f"http://{host}:{port}{path}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def browser_websocket_url(endpoint: ResolvedEndpoint) -> str:
    """Read one HTTP endpoint's browser WebSocket URL, re-validated.

    Args:
        endpoint: An endpoint whose ``websocket_url`` is ``None``, i.e. the
            ``--endpoint http://HOST:PORT`` form.

    Returns:
        The browser WebSocket URL, checked against the loopback rule.

    Raises:
        ConnectionError: Nothing answered, the answer was not a DevTools
            document, or the peer named an address ``bt`` will not dial.
    """
    source = f"{endpoint.flag} {endpoint.host}:{endpoint.port}"
    try:
        document = fetch_devtools_json(endpoint.host, endpoint.port, "/json/version")
    except (OSError, ValueError) as exc:
        from .one_shot import connection_failure_message

        raise ConnectionError(connection_failure_message(endpoint, exc)) from exc
    if not isinstance(document, dict):
        raise ConnectionError(f"{source} answered /json/version with a non-JSON-object body.")
    return validate_peer_websocket_url(document.get("webSocketDebuggerUrl"), source=source)
