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
Untrusted keys and aliases are omitted from diagnostics as well.
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
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

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
            _refuse("login config has a duplicate key")
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
    """Parse an exact HTTPS origin, including bracketed IPv6 addresses."""
    from ipaddress import IPv6Address
    from urllib.parse import urlsplit

    if not isinstance(value, str) or not value or len(value) > MAX_ORIGIN_CHARS:
        _refuse("login config field 'origin' must be an HTTPS origin string")
    if any(ord(char) <= 32 for char in value) or "?" in value or "#" in value:
        _refuse("login config field 'origin' must have no whitespace, query or fragment")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        _refuse("login config field 'origin' is not a valid HTTPS origin")
    if parts.scheme != "https" or not host:
        _refuse("login config field 'origin' must be an https:// origin")
    if parts.username is not None or parts.password is not None:
        _refuse("login config field 'origin' must not carry user info")
    if parts.path not in ("", "/"):
        _refuse("login config field 'origin' must have no path")
    if ":" in host:
        if "%" in host:
            _refuse("login config field 'origin' must not contain an IPv6 zone")
        try:
            host = f"[{IPv6Address(host).compressed}]"
        except ValueError:
            _refuse("login config field 'origin' has an invalid IPv6 address")
    else:
        raw_host = parts.netloc.split(":", 1)[0]
        if raw_host != host or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
            _refuse("login config field 'origin' host must be lowercase ASCII")
        if ".." in host or ".-" in host or "-." in host:
            _refuse("login config field 'origin' host has an invalid label")
    if port is not None and not 1 <= port <= 65535:
        _refuse("login config field 'origin' port is out of range")
    return f"https://{host}" if port is None or port == 443 else f"https://{host}:{port}"


def _validate_account(alias: str, raw: Any) -> AccountSpec:
    if not isinstance(raw, dict):
        _refuse("login config account must be an object")
    unknown = sorted(set(raw) - _ACCOUNT_FIELDS)
    if unknown:
        _refuse(
            "login config account has an unknown field. Check the documented schema"
        )
    missing = sorted(_ACCOUNT_FIELDS - set(raw))
    if missing:
        _refuse(
            f"login config account is missing field(s): {', '.join(missing)}"
        )
    item = raw["item"]
    if not isinstance(item, str) or not _UUID_RE.fullmatch(item):
        _refuse(
            "login config account field 'item' must be a 1Password item ID "
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
    except LoginPolicyError:
        raise
    except (ValueError, RecursionError):
        _refuse("login config is not valid JSON")
    if not isinstance(document, dict):
        _refuse(f"login config at {path} must be a JSON object")
    unknown = sorted(set(document) - _TOP_LEVEL_FIELDS)
    if unknown:
        _refuse(
            "login config has an unknown field. Check the documented schema"
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
        raise LoginPolicyError("no login account is configured for the requested alias")
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
