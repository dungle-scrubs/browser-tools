"""The operator-controlled login policy: fail-closed before any provider access.

``bt login --account ALIAS`` refuses to guess. Everything it is allowed to do
with credentials -- which vault item, which origin, which selectors -- comes
from one JSON file the operator edits by hand:

    $XDG_CONFIG_HOME/browser-tools/logins.json
    (~/.config/browser-tools/logins.json when XDG_CONFIG_HOME is unset)

``--config FILE`` overrides the path. The file stores *references and
selectors*, never resolved secrets; the secret values exist only inside the
private opchain relay (see :mod:`browser_tools.credential_relay`) and the
form's own fields, never in this module.

Fail-closed is the whole design: a missing, unreadable, oversized, duplicate-
key, malformed, unsupported-version or structurally invalid policy is a usage
error (CLI exit 2) raised *before* the browser is contacted and long before any
credential is read. The config is editable by the same OS user, so it is an
accidental-use restriction, not a defense against that user or their
unrestricted agents.

Error messages name the field that failed, never the value it held: a
mis-typed selector or vault UUID is not printed into a terminal or log.
Aliases are the one exception, because an alias is what the operator types on
the command line and is the only way to say *which* account entry is broken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from .usage import UsageError

#: The only policy version this build understands.
POLICY_VERSION = 1

#: Bounds. A policy beyond any of these is refused, not truncated.
MAX_POLICY_BYTES = 256 * 1024
MAX_ACCOUNTS = 64
MAX_IDENTITY_CHARS = 64
MAX_SELECTOR_CHARS = 256
MAX_ORIGIN_CHARS = 2048

#: 1Password vault/item IDs: 26 lowercase base32 characters.
_UUID_RE = re.compile(r"^[a-z2-7]{26}$")

#: Identity and account aliases: ASCII letters, digits, underscores and
#: hyphens, no leading hyphen, non-empty, bounded.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: The two field names this operation is allowed to read from the item.
USERNAME_FIELD = "username"
PASSWORD_FIELD = "password"

_TOP_LEVEL_FIELDS = frozenset({"version", "identity", "vault", "accounts"})
_ACCOUNT_FIELDS = frozenset(
    {
        "item",
        "origin",
        "username_selector",
        "password_selector",
        "submit_selector",
        "success_selector",
    }
)


class LoginPolicyError(UsageError):
    """A policy problem: malformed, unreadable, or structurally invalid.

    Maps to CLI exit 2, like every :class:`browser_tools.usage.UsageError`.
    The message is a fixed sentence naming the field, never its value.
    """


@dataclass(frozen=True)
class AccountSpec:
    """One configured login: where to read the secret, where to type it.

    Carries references and selectors only. No secret value ever reaches this
    dataclass, so its repr is safe by construction.
    """

    alias: str
    item: str
    origin: str
    username_selector: str
    password_selector: str
    submit_selector: str
    success_selector: str


@dataclass(frozen=True)
class LoginPolicy:
    """The whole validated policy file."""

    identity: str
    vault: str
    accounts: dict[str, AccountSpec]

    def account(self, alias: str) -> AccountSpec | None:
        """The configured account for ``alias``, or None when not configured."""
        return self.accounts.get(alias)

    def field_reference(self, account: AccountSpec, field: str) -> str:
        """The exact ``op://VAULT/ITEM/FIELD`` reference for one credential.

        The policy cannot choose another field, vault or item per invocation:
        the reference is constructed here from the validated policy, never
        assembled from caller input.
        """
        if field not in (USERNAME_FIELD, PASSWORD_FIELD):
            raise LoginPolicyError("internal: unknown credential field")
        return f"op://{self.vault}/{account.item}/{field}"


def policy_path(config: str | None = None) -> Path:
    """The policy file path: ``--config`` override, else the XDG config dir.

    The same ``$XDG_CONFIG_HOME/browser-tools`` directory ``user_settings``
    already uses; this is one more file in it, not a new project layout.
    """
    import os

    if config is not None:
        return Path(config)
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "browser-tools" / "logins.json"


def _refuse(message: str) -> NoReturn:
    raise LoginPolicyError(message)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` hook that refuses a repeated key anywhere in the file.

    JSON's last-wins rule would silently hide a duplicated account or vault
    behind the copy that happens to sit later in the file. The operator wrote
    both; refusing is the only answer that cannot pick the wrong one.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            _refuse(f"login config has a duplicate key: '{key}'")
        seen[key] = value
    return seen


def _require_name(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value) or len(value) > limit:
        _refuse(
            f"login config field '{field}' must be 1-{limit} ASCII letters, digits, "
            "underscores or hyphens, with no leading hyphen"
        )
    return value


def _require_selector(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SELECTOR_CHARS
        or any(ch in value for ch in "\n\r\t")
    ):
        _refuse(
            f"login config field '{field}' must be a non-empty selector string "
            f"of at most {MAX_SELECTOR_CHARS} characters with no line breaks"
        )
    return value


def _canonical_origin(value: Any) -> str:
    """Validate and canonicalize one configured origin.

    Accepted: ``https://host`` or ``https://host:port`` with an explicit port,
    where host is already lowercase ASCII (letters, digits, dots, hyphens).
    Rejected: userinfo, any path except empty or ``/``, a query, a fragment,
    wildcard hosts, opaque URLs, and every scheme other than https.

    Canonical form keeps an explicit non-default port and drops ``:443``,
    matching what ``location.origin`` reports in the browser, so the two sides
    compare for exact equality with no per-invocation normalization.
    """
    from urllib.parse import urlsplit

    if not isinstance(value, str) or not value or len(value) > MAX_ORIGIN_CHARS:
        _refuse("login config field 'origin' must be an HTTPS origin string")
    try:
        parts = urlsplit(value)
    except ValueError:
        _refuse("login config field 'origin' is not a valid HTTPS origin")
    if parts.scheme != "https" or not parts.netloc:
        _refuse("login config field 'origin' must be an https:// origin")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        _refuse(
            "login config field 'origin' must be a bare origin: no path, query "
            "or fragment"
        )
    netloc = parts.netloc
    if "@" in netloc:
        _refuse("login config field 'origin' must not carry user info")
    host, sep, port_text = netloc.rpartition(":")
    if not sep:
        host, port_text = netloc, ""
    # An IPv6 literal contains ':' inside brackets; reject rather than guess a
    # canonical text form for it.
    if host.startswith("[") or ":" in host:
        _refuse(
            "login config field 'origin' must be a plain hostname or address, "
            "not an IPv6 literal"
        )
    if not host or host != host.lower() or not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", host):
        _refuse(
            "login config field 'origin' host must be lowercase ASCII letters, "
            "digits, dots and hyphens"
        )
    if ".." in host or ".-" in host or "-." in host:
        _refuse("login config field 'origin' host has an empty or invalid label")
    port: int | None = None
    if port_text:
        if not port_text.isdigit():
            _refuse("login config field 'origin' port must be numeric")
        port = int(port_text)
        if not 1 <= port <= 65535:
            _refuse("login config field 'origin' port is out of range")
    if port is None or port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


def _validate_account(alias: str, raw: Any) -> AccountSpec:
    if not isinstance(raw, dict):
        _refuse(f"login config account '{alias}' must be an object")
    unknown = sorted(set(raw) - _ACCOUNT_FIELDS)
    if unknown:
        _refuse(
            f"login config account '{alias}' has unknown field(s): {', '.join(unknown)}. "
            "Unknown fields are refused to catch misspellings"
        )
    missing = sorted(_ACCOUNT_FIELDS - set(raw))
    if missing:
        _refuse(
            f"login config account '{alias}' is missing field(s): {', '.join(missing)}"
        )
    item = raw["item"]
    if not isinstance(item, str) or not _UUID_RE.fullmatch(item):
        _refuse(
            f"login config account '{alias}' field 'item' must be a 1Password item ID "
            "(26 lowercase base32 characters)"
        )
    return AccountSpec(
        alias=alias,
        item=item,
        origin=_canonical_origin(raw["origin"]),
        username_selector=_require_selector(raw["username_selector"], "username_selector"),
        password_selector=_require_selector(raw["password_selector"], "password_selector"),
        submit_selector=_require_selector(raw["submit_selector"], "submit_selector"),
        success_selector=_require_selector(raw["success_selector"], "success_selector"),
    )


def load_policy(path: Path) -> LoginPolicy:
    """Read and fully validate the policy at ``path``.

    Raises:
        LoginPolicyError: The file is missing, unreadable, oversized, not
            valid UTF-8, duplicate-keyed, malformed JSON, an unsupported
            version, or structurally invalid. Nothing is read from 1Password
            and no browser is contacted before this returns.
    """

    def field(name: str) -> str:
        return f"login config field '{name}'"

    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_POLICY_BYTES + 1)
    except FileNotFoundError:
        _refuse(f"login config not found at {path}. Create it or pass --config FILE")
    except OSError:
        _refuse(f"login config at {path} could not be read")
    if len(data) > MAX_POLICY_BYTES:
        _refuse(f"login config at {path} exceeds {MAX_POLICY_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(f"login config at {path} is not valid UTF-8")
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError:
        _refuse(f"login config at {path} is not valid JSON")
    if not isinstance(document, dict):
        _refuse(f"login config at {path} must be a JSON object")
    unknown = sorted(set(document) - _TOP_LEVEL_FIELDS)
    if unknown:
        _refuse(
            f"login config has unknown field(s): {', '.join(unknown)}. "
            "Unknown fields are refused to catch misspellings"
        )
    missing = sorted(_TOP_LEVEL_FIELDS - set(document))
    if missing:
        _refuse(f"login config is missing field(s): {', '.join(missing)}")
    version = document["version"]
    # bool first: in Python, True == 1, so a JSON true would pass an int check.
    if isinstance(version, bool) or version != POLICY_VERSION:
        _refuse(
            f"login config 'version' must be {POLICY_VERSION}; this build "
            "refuses versions it does not define"
        )
    identity = _require_name(document["identity"], "identity", MAX_IDENTITY_CHARS)
    vault = document["vault"]
    if not isinstance(vault, str) or not _UUID_RE.fullmatch(vault):
        _refuse(
            f"{field('vault')} must be a 1Password vault ID "
            "(26 lowercase base32 characters)"
        )
    accounts_raw = document["accounts"]
    if not isinstance(accounts_raw, dict) or not accounts_raw:
        _refuse("login config field 'accounts' must be a non-empty object")
    if len(accounts_raw) > MAX_ACCOUNTS:
        _refuse(f"login config accepts at most {MAX_ACCOUNTS} accounts")
    accounts: dict[str, AccountSpec] = {}
    for alias, raw in accounts_raw.items():
        _require_name(alias, "account alias", MAX_IDENTITY_CHARS)
        accounts[alias] = _validate_account(alias, raw)
    return LoginPolicy(identity=identity, vault=vault, accounts=accounts)


def load_account(config: str | None, alias: str) -> tuple[LoginPolicy, AccountSpec]:
    """Load the policy and return it with the one account ``alias`` names.

    The single entry point ``bt login`` uses: policy first, alias second, so
    an unknown alias fails exactly as closed as a malformed policy.
    """
    policy = load_policy(policy_path(config))
    account = policy.account(alias)
    if account is None:
        known = ", ".join(sorted(policy.accounts)) or "(none)"
        raise LoginPolicyError(
            f"no login account named '{alias}' is configured. Configured: {known}"
        )
    return policy, account


__all__ = [
    "MAX_ACCOUNTS",
    "MAX_POLICY_BYTES",
    "PASSWORD_FIELD",
    "POLICY_VERSION",
    "USERNAME_FIELD",
    "AccountSpec",
    "LoginPolicy",
    "LoginPolicyError",
    "load_account",
    "load_policy",
    "policy_path",
]
