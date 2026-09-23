"""Raw CDP passthrough and live-schema help for the merged CLI front (RFC-01 #37).

New layer-4 code, alongside ``cli.py`` and ``lifecycle.py``. It owns:

- **Passthrough dispatch.** ``[INSTANCE] Domain.method '{...}' [--target SPEC]``
  sends any CDP method the installed browser supports straight to it and
  returns the raw JSON result. It opens the browser-level connection,
  resolves a page target, attaches an isolated ``Target`` session, and sends
  the call through ``core.cdp_client.CDPClient.send`` -- the same client
  ``send`` path any future curated tool would use (RFC-01, "layer 2 tools
  MUST call the same CDP client `send` path the passthrough uses").

- **Live-schema help.** ``help [INSTANCE] [Domain.method]`` reads the CDP
  protocol schema live from a running browser's ``/json/protocol`` endpoint
  (``core.protocol.discover_protocol``) when exactly one instance can be
  resolved, and prints static usage otherwise. The port is resolved by this
  module rather than delegated to ``core.protocol``'s own instance/port
  resolution, which always reads the vendored default registry path
  (``/tmp/chrome-agent/registry.json``) and ignores the
  ``BROWSER_TOOLS_REGISTRY`` override every other verb honors. Resolving the
  port here keeps ``core/protocol.py`` untouched (RFC-01: prefer the adapted
  core modules unchanged) while still respecting the registry override.

- **Instance-vs-method disambiguation.** A bare leading CLI token resolves as
  an instance name if the registry knows it, else as a ``Domain.method``
  (RFC-01, "Instance names"). ``INSTANCE`` is omittable when exactly one
  instance is registered, reusing ``lifecycle.resolve_single_instance``
  (#35/#36).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import endpoint as browser_endpoint
from . import lifecycle
from .core import protocol as core_protocol
from .core import registry as core_registry
from .core.registry import InstanceNotFoundError
from .endpoint import ResolvedEndpoint
from .lifecycle import LifecycleError
from .one_shot import cli_cdp_errors, one_shot_page_session
from .usage import UsageError as BaseUsageError


class UsageError(BaseUsageError):
    """A malformed raw-protocol or help invocation (CLI exit code 2)."""


# ---------------------------------------------------------------------------
# Instance-vs-method disambiguation
# ---------------------------------------------------------------------------


def is_passthrough_head(token: str, registry_path: str | None = None) -> bool:
    """Whether a bare leading CLI token should route to raw-protocol dispatch.

    True when the registry knows ``token`` as an instance name, or when it
    has the shape of a ``Domain.method`` token. The CLI front uses this to
    decide whether an unrecognized leading token is raw-protocol dispatch or
    a genuinely unknown verb (which stays an argparse usage error).
    """
    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    if token in known:
        return True
    return lifecycle.looks_like_domain_method(token)


def resolve_passthrough_args(
    args: list[str],
    registry_path: str | None = None,
) -> tuple[str | None, str, str | None]:
    """Disambiguate the raw-protocol line's leading token.

    ``args`` is the raw-protocol argv with ``--target``/``--url`` already
    extracted (see ``extract_target_flags``). Per RFC-01 "Instance names": a
    bare leading token resolves as an instance name if the registry knows it,
    else as a ``Domain.method``. Returns
    ``(instance_or_None, method, params_json_or_None)``.

    Raises ``UsageError`` when nothing was given, when a known instance name
    is not followed by a method, or when the method position does not look
    like ``Domain.method``.
    """
    if not args:
        raise UsageError("expected Domain.method (optionally preceded by INSTANCE)")

    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    head, tail = args[0], args[1:]

    if head in known:
        if not tail:
            raise UsageError(f"expected Domain.method after instance name '{head}'")
        method, params_json = tail[0], (tail[1] if len(tail) > 1 else None)
        if not lifecycle.looks_like_domain_method(method):
            raise UsageError(f"'{method}' does not look like Domain.method")
        return head, method, params_json

    if not lifecycle.looks_like_domain_method(head):
        raise UsageError(f"'{head}' is neither a known instance nor a Domain.method")
    params_json = tail[0] if tail else None
    return None, head, params_json


def resolve_help_args(
    args: list[str],
    registry_path: str | None = None,
) -> tuple[str | None, str | None]:
    """Disambiguate ``help [INSTANCE] [Domain.method]``.

    Same rule as the raw-protocol line: the leading token is an instance name
    if the registry knows it, else the query itself (a bare domain or a
    ``Domain.method``). Returns ``(instance_or_None, query_or_None)``.
    """
    if not args:
        return None, None
    if len(args) > 2:
        raise UsageError("help takes at most [INSTANCE] [Domain.method]")

    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    head = args[0]
    if head in known:
        return head, (args[1] if len(args) > 1 else None)
    if len(args) > 1:
        raise UsageError(f"'{head}' is not a known instance; only a query may follow it")
    return None, head


def strip_endpoint_flag(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull ``--endpoint URL`` out of an argv, wherever it sits.

    The raw-protocol line bypasses argparse, so nothing else would find the
    flag there. The CLI front also calls this before deciding whether a leading
    token is a verb, so ``bt --endpoint URL Page.navigate '{...}'`` reads the
    same as ``bt Page.navigate '{...}' --endpoint URL``.

    Returns ``(remaining_args, endpoint_or_None)``.
    """
    remaining: list[str] = []
    endpoint: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--endpoint" and i + 1 < len(argv):
            endpoint = argv[i + 1]
            i += 2
        else:
            remaining.append(argv[i])
            i += 1
    return remaining, endpoint


def _is_profile_value(token: str, verbs: set[str] | frozenset[str]) -> bool:
    """Whether the token after a bare ``--chrome-profile`` is its value.

    The flag's value is optional, so the token after it belongs to the flag
    only when it cannot belong to the invocation. Four things it is never:
    another flag, a verb, a ``Domain.method``, and a JSON operand. The operand
    test is the one the first version lacked, and
    ``bt Runtime.evaluate --chrome-profile '{"expression":"1"}'`` therefore
    reported the params as a bad channel name instead of running.
    """
    return not (
        token.startswith("-")
        or token.startswith("{")
        or token in verbs
        or lifecycle.looks_like_domain_method(token)
    )


def strip_chrome_profile_flag(
    argv: list[str], *, verbs: set[str] | frozenset[str] = frozenset()
) -> tuple[list[str], str | None]:
    """Extract the optional profile value without consuming what follows it.

    The value is a user data directory path, or a channel name. It is not
    validated here: a path this layer cannot check is a path
    ``chrome_discovery`` reports on with the directory in hand, and refusing a
    value here means refusing it before the invocation's own refusals have
    been reached. ``bt profile --chrome-profile list`` reported an unknown
    channel for that reason, when what is wrong with it is that ``profile``
    does not take the flag at all.
    """
    remaining: list[str] = []
    profile: str | None = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--chrome-profile" or token.startswith("--chrome-profile="):
            if profile is not None:
                raise UsageError("--chrome-profile may be given only once")
            profile = token.split("=", 1)[1] if "=" in token else "stable"
            if (
                token == "--chrome-profile"
                and i + 1 < len(argv)
                and _is_profile_value(argv[i + 1], verbs)
            ):
                i += 1
                profile = argv[i]
        else:
            remaining.append(token)
        i += 1
    return remaining, profile


async def send_on_session(
    cdp: Any,
    session_id: str,
    method: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Send one method over a session someone else opened.

    Split out so a Step Run can reach it: the run holds one session for every
    step, so it cannot call a function that opens its own. The hidden-tab
    refusal travels with the send rather than being left to the caller,
    because a step must not be able to reach the browser around it.
    """
    if method.startswith(_INPUT_DELIVERY_PREFIXES):
        await _refuse_input_to_hidden_tab(cdp, session_id)
    return await cdp.send(method=method, params=params, session_id=session_id)


def extract_target_flags(
    argv: list[str],
) -> tuple[list[str], str | None, str | None, str | ResolvedEndpoint | None]:
    """Pull ``--target`` / ``--url`` / ``--endpoint`` out of a passthrough argv.

    They may appear anywhere in the invocation. Returns
    ``(remaining_args, target, url, endpoint)``. Raises ``UsageError`` if both
    ``--target`` and ``--url`` are given -- they select the same slot and
    cannot both be honored.
    """
    from .cli import KNOWN_VERBS

    argv, profile = strip_chrome_profile_flag(argv, verbs=KNOWN_VERBS)
    argv, endpoint = strip_endpoint_flag(argv)
    if profile is not None:
        if endpoint is not None:
            raise UsageError("--endpoint and --chrome-profile cannot be combined")
        # The refusal comes before discovery reaches out, not after. Discovery
        # now reads /json/version from the browser, and an invocation that can
        # never run should not touch it at all -- nor should it answer a
        # refused method with a discovery failure. The method has not been
        # resolved yet at this point, so the two refused names are looked for
        # where they can only be the method.
        for token in argv:
            browser_endpoint.refuse_browser_lifetime_method(token)
        from .chrome_discovery import discover_chrome

        endpoint = discover_chrome(profile)
    remaining: list[str] = []
    target: str | None = None
    url: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--target" and i + 1 < len(argv):
            target = argv[i + 1]
            i += 2
        elif argv[i] == "--url" and i + 1 < len(argv):
            url = argv[i + 1]
            i += 2
        else:
            remaining.append(argv[i])
            i += 1
    if target is not None and url is not None:
        raise UsageError("cannot specify both --target and --url")
    return remaining, target, url, endpoint


# ---------------------------------------------------------------------------
# Focus guard
# ---------------------------------------------------------------------------

#: Methods that raise the browser window over whatever the person at the
#: machine is doing. Measured on macOS (Chrome 153): each one makes Chrome the
#: active app and moves the window manager's focus to it. No task needs them:
#: input and screenshots reach the selected tab of a window that is behind
#: other windows or on another workspace.
#:
#: ``Browser.setWindowBounds`` is not here. It was refused on the assumption
#: that it belongs with these, and the measurement says otherwise: resizing,
#: minimizing and restoring a window all left the focus where it was.
FOCUS_TAKING_METHODS = frozenset({"Target.activateTarget", "Page.bringToFront"})

#: Input methods that Chrome drops without an error when the target tab is a
#: background tab (not the selected tab of its window). Measured: a click sent
#: to a background tab reaches neither the top document nor a cross-origin
#: iframe. The silent drop is what makes an agent reach for
#: ``Target.activateTarget``, so these fail loudly instead.
_INPUT_DELIVERY_PREFIXES = (
    "Input.dispatch",
    "Input.insertText",
    "Input.imeSetComposition",
    "Input.synthesize",
)

WORK_IN_THE_BACKGROUND = (
    "Input and screenshots reach the selected tab of a window even when the "
    "window is behind other windows. To work in another page, open it in its "
    "own window with Target.createTarget '{\"url\": \"...\", \"newWindow\": true}' "
    "and pass the new targetId as --target, or navigate the current tab with "
    "Page.navigate."
)


#: The one spelling of the hidden-tab refusal. The curated verbs raise the
#: same text through ``LifecycleError``, so both surfaces give one answer.
HIDDEN_TAB_MESSAGE = (
    "The target is a background tab (document.visibilityState is "
    "'hidden'). Chrome drops input sent to it without an error. Do not "
    "activate the tab: that raises the browser window over the user's "
    f"work. {WORK_IN_THE_BACKGROUND}"
)


class HiddenTargetError(Exception):
    """Input was sent to a background tab, which Chrome silently ignores."""


def guard_focus(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Refuse calls that take the screen; return the params to send.

    ``Target.createTarget`` always opens in the background: ``background`` is
    set to true when omitted, and an explicit ``false`` is refused. Chrome's
    default is foreground, which activates the new tab and raises its window.
    """
    if method in FOCUS_TAKING_METHODS:
        raise UsageError(
            f"{method} is refused: it raises the browser window over the user's "
            f"work. {WORK_IN_THE_BACKGROUND}"
        )
    if method == "Target.createTarget":
        if params is not None and params.get("background") is False:
            raise UsageError(
                "Target.createTarget with background:false is refused: a foreground "
                "tab raises the browser window over the user's work. Omit "
                "background; it is always set to true."
            )
        return {**(params or {}), "background": True}
    return params


async def _refuse_input_to_hidden_tab(cdp: Any, session_id: str) -> None:
    """Raise ``HiddenTargetError`` when the attached page is a background tab."""
    result = await cdp.send(
        method="Runtime.evaluate",
        params={"expression": "document.visibilityState", "returnByValue": True},
        session_id=session_id,
    )
    if result.get("result", {}).get("value") == "hidden":
        raise HiddenTargetError(HIDDEN_TAB_MESSAGE)


# ---------------------------------------------------------------------------
# Passthrough dispatch
# ---------------------------------------------------------------------------


@cli_cdp_errors
def send(
    *,
    instance: str | None,
    method: str,
    params_json: str | None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
    endpoint: str | ResolvedEndpoint | None = None,
) -> dict[str, Any]:
    """Send one raw CDP ``Domain.method`` call and return its JSON result.

    ``instance`` omitted resolves via ``lifecycle.resolve_single_instance``
    (fails naming the candidates unless exactly one instance is registered).
    ``endpoint`` drives an external browser instead, and then no registry entry
    is read or written. ``params_json`` must parse to a JSON object; a parse
    failure or a non-object payload is a ``UsageError`` (CLI exit 2). CDP-level
    and target-resolution failures become ``LifecycleError`` (CLI exit 1), via
    ``@cli_cdp_errors``.

    Over an external endpoint ``Browser.close`` and ``Browser.crash`` are
    refused: they would end every window and tab of a browser this tool does
    not own. Everything else passes, ``Target.closeTarget`` included.
    """
    if endpoint is not None:
        browser_endpoint.refuse_browser_lifetime_method(method)
    port = lifecycle.resolve_cdp_port(instance, registry_path, endpoint)

    params: dict[str, Any] | None = None
    if params_json is not None:
        try:
            parsed = json.loads(params_json)
        except json.JSONDecodeError as exc:
            raise UsageError(f"invalid JSON parameters: {exc}") from exc
        if not isinstance(parsed, dict):
            raise UsageError("parameters must be a JSON object")
        params = parsed
    params = guard_focus(method, params)

    target_by: str | None = None
    spec: str | None = None
    if target is not None:
        spec = target
        target_by = "index" if target.isdigit() else "id"
    elif url is not None:
        spec = url
        target_by = "url"

    async def _send() -> dict[str, Any]:
        async with one_shot_page_session(
            port, spec, target_by
        ) as (cdp, session_id):
            return await send_on_session(cdp, session_id, method, params)

    try:
        return asyncio.run(_send())
    except HiddenTargetError as exc:
        raise LifecycleError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Live-schema help
# ---------------------------------------------------------------------------

STATIC_HELP = """\
browser-tools / bt -- CDP protocol help

No running browser instance is available to answer this query, so this is
static usage rather than the live protocol schema read from a browser.

  help [INSTANCE] [Domain.method]
      With a running instance, prints the live CDP protocol schema fetched
      from that browser: every domain, one domain's commands and events, or
      one method's full parameter and return signature. INSTANCE may be
      omitted when exactly one instance is running.

  [INSTANCE] Domain.method '{...json params...}' [--target SPEC]
      Send any CDP method the installed browser supports straight to it and
      print the JSON result to stdout. No curated tool is required to exist
      for the method. INSTANCE may be omitted when exactly one instance is
      running; with several running, name one explicitly. Methods that raise
      the browser window over the user's work (Target.activateTarget and
      Page.bringToFront) are refused, and
      Target.createTarget always opens in the background. To work in a second
      page, open it with '{"url": "...", "newWindow": true}' and pass its
      targetId as --target. Run `bt guide` for details.
"""

#: Added to the static help when no browser was named at all. It is left off
#: for an external endpoint: `bt launch` starts a browser bt owns, which is not
#: the browser the person is attaching to, and following it would open a second
#: one instead of answering the question.
LAUNCH_TRAILER = "\nLaunch a browser first: bt launch\n"

#: Added instead when an endpoint was named and could not serve the schema.
#: `/json/protocol` is an HTTP path, and a Chrome 144+ approval service answers
#: 404 for every `/json` path by design.
ENDPOINT_TRAILER = (
    "\nThe browser at {host}:{port} did not serve /json/protocol, so this is static\n"
    "usage. A Chrome that only exposes the approval-based debugging service answers\n"
    "404 for every /json path, including this one.\n"
)


def _resolve_help_port(
    instance: str | None, registry_path: str | None, endpoint: str | ResolvedEndpoint | None = None
) -> int | ResolvedEndpoint | None:
    """Resolve the port to query for live help, or None for static usage."""
    if endpoint is not None:
        return endpoint if isinstance(endpoint, ResolvedEndpoint) else browser_endpoint.resolve_endpoint_port(endpoint)
    if instance is not None:
        try:
            info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
        except InstanceNotFoundError as exc:
            raise LifecycleError(str(exc)) from exc
        return info.port

    # No instance named: only auto-resolve when exactly one instance is
    # actually live (engine-aware). Zero or several live instances means
    # there is nothing unambiguous to query, so this falls back to static
    # usage rather than guessing.
    instances = lifecycle.read_instances(registry_path=registry_path)
    live = [i for i in instances if lifecycle.instance_is_live(i)]
    if len(live) == 1:
        return live[0].port
    return None


def _print_external_protocol(endpoint: ResolvedEndpoint, query: str | None) -> bool:
    """Print the live schema an external endpoint serves; say whether it did.

    An endpoint reached over a browser WebSocket still has an HTTP side at the
    same address: discovery just read ``/json/version`` there, and Chrome
    serves ``/json/protocol`` beside it. The first version refused live help
    for every external endpoint, so a person attaching got static text that
    ended by telling them to launch a browser.

    The schema is fetched here rather than through the vendored
    ``core.protocol.discover_protocol``, which requests ``localhost`` and would
    reach the wrong loopback family; the vendored formatting is reused as it
    stands.
    """
    from .devtools_http import SCHEMA_TIMEOUT, fetch_devtools_json

    try:
        schema = fetch_devtools_json(
            endpoint.host, endpoint.port, "/json/protocol", timeout=SCHEMA_TIMEOUT
        )
        domains = schema["domains"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    # Past this point a schema is in hand, so an unresolvable query is the
    # caller's usage error and must not be read as an unreachable endpoint.
    if query is None:
        core_protocol._print_all_domains(domains=domains)  # pyright: ignore[reportPrivateUsage]
    elif "." not in query:
        core_protocol._print_domain_detail(domains=domains, domain_name=query)  # pyright: ignore[reportPrivateUsage]
    else:
        core_protocol._print_method_detail(domains=domains, query=query)  # pyright: ignore[reportPrivateUsage]
    return True


def run_help(
    instance: str | None,
    query: str | None,
    registry_path: str | None = None,
    endpoint: str | ResolvedEndpoint | None = None,
) -> None:
    """Print live-schema help from a running instance, else static usage.

    Raises ``UsageError`` for an unresolvable ``Domain``/``Domain.method``
    query against a schema that was successfully fetched.
    """
    port = _resolve_help_port(instance, registry_path, endpoint)
    if port is None:
        print(STATIC_HELP, end="")
        print(LAUNCH_TRAILER, end="")
        return
    if isinstance(port, ResolvedEndpoint):
        try:
            if _print_external_protocol(port, query):
                return
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        print(STATIC_HELP, end="")
        print(ENDPOINT_TRAILER.format(host=port.host, port=port.port), end="")
        return
    try:
        core_protocol.discover_protocol(port=port, query=query)
    except ConnectionError:
        print(STATIC_HELP, end="")
        print(LAUNCH_TRAILER, end="")
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
