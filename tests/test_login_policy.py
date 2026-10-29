"""The login policy: everything ``bt login`` is allowed to do, validated first.

The policy is the security boundary's address book: which vault item, which
origin, which selectors. These tests hold it to the fail-closed rule -- every
malformed shape is refused before any browser or provider access, with a
diagnostic that names the field and never its value.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from browser_tools.login_policy import (
    MAX_ACCOUNTS,
    MAX_POLICY_BYTES,
    AccountSpec,
    LoginPolicyError,
    load_account,
    load_policy,
    policy_path,
)

VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"


def valid_document() -> dict:
    return {
        "version": 1,
        "identity": "browser",
        "vault": VAULT,
        "accounts": {
            "example": {
                "item": ITEM,
                "origin": "https://example.com",
                "username_selector": "#username",
                "password_selector": "#password",
                "submit_selector": "button[type=submit]",
                "success_selector": "[data-signed-in]",
            }
        },
    }


def write_policy(tmp_path: Path, document: dict | str, name: str = "logins.json") -> Path:
    path = tmp_path / name
    if isinstance(document, str):
        path.write_text(document)
    else:
        path.write_text(json.dumps(document, indent=2))
    return path


class TestValidPolicy:
    def test_a_valid_document_loads_with_all_fields(self, tmp_path):
        policy = load_policy(write_policy(tmp_path, valid_document()))
        assert policy.identity == "browser"
        assert policy.vault == VAULT
        account = policy.account("example")
        assert account == AccountSpec(
            alias="example",
            item=ITEM,
            origin="https://example.com",
            username_selector="#username",
            password_selector="#password",
            submit_selector="button[type=submit]",
            success_selector="[data-signed-in]",
        )

    def test_references_are_constructed_not_configured(self, tmp_path):
        policy = load_policy(write_policy(tmp_path, valid_document()))
        account = policy.account("example")
        assert policy.field_reference(account, "username") == f"op://{VAULT}/{ITEM}/username"
        assert policy.field_reference(account, "password") == f"op://{VAULT}/{ITEM}/password"

    def test_field_reference_refuses_any_other_field(self, tmp_path):
        policy = load_policy(write_policy(tmp_path, valid_document()))
        with pytest.raises(LoginPolicyError):
            policy.field_reference(policy.account("example"), "otp")

    def test_policy_dataclasses_hold_configuration_only(self, tmp_path):
        """References and selectors are configuration; no secret value exists here."""
        policy = load_policy(write_policy(tmp_path, valid_document()))
        account = policy.account("example")
        assert account.alias in repr(account)
        assert account.username_selector in repr(account)
        assert policy.field_reference(account, "username") == f"op://{VAULT}/{ITEM}/username"


class TestMalformedBoundaryValues:
    @pytest.mark.parametrize("origin", ["https://[", "https://[::1"])
    def test_malformed_url_is_a_usage_error(self, tmp_path, origin):
        document = valid_document()
        document["accounts"]["example"]["origin"] = origin
        with pytest.raises(LoginPolicyError):
            load_policy(write_policy(tmp_path, document))

    @pytest.mark.parametrize("field", ["vault", "identity", "item"])
    def test_trailing_newline_is_not_part_of_an_identifier(self, tmp_path, field):
        document = valid_document()
        target = document["accounts"]["example"] if field == "item" else document
        target[field] += "\n"
        with pytest.raises(LoginPolicyError):
            load_policy(write_policy(tmp_path, document))


class TestFileLevelFailures:
    def test_missing_file(self, tmp_path):
        with pytest.raises(LoginPolicyError, match="not found"):
            load_policy(tmp_path / "logins.json")

    def test_unreadable_file(self, tmp_path):
        path = write_policy(tmp_path, valid_document())
        path.chmod(0o000)
        try:
            with pytest.raises(LoginPolicyError, match="could not be read"):
                load_policy(path)
        finally:
            path.chmod(0o644)

    def test_oversized_file(self, tmp_path):
        path = tmp_path / "logins.json"
        path.write_bytes(b"x" * (MAX_POLICY_BYTES + 1))
        with pytest.raises(LoginPolicyError, match="exceeds"):
            load_policy(path)

    def test_not_json(self, tmp_path):
        with pytest.raises(LoginPolicyError, match="not valid JSON"):
            load_policy(write_policy(tmp_path, "{"))

    def test_not_an_object(self, tmp_path):
        with pytest.raises(LoginPolicyError, match="must be a JSON object"):
            load_policy(write_policy(tmp_path, "[1, 2]"))

    def test_not_utf8(self, tmp_path):
        path = tmp_path / "logins.json"
        path.write_bytes(b'{"version": 1, "\xff": 1}')
        with pytest.raises(LoginPolicyError, match="UTF-8"):
            load_policy(path)

    def test_duplicate_key_anywhere(self, tmp_path):
        text = json.dumps(valid_document())
        text = text.replace('{"version"', '{"version"', 1)
        # Splice a duplicate top-level key after the object opens.
        duplicated = '{' + '"version": 2, ' + text[1:]
        with pytest.raises(LoginPolicyError, match="duplicate key: 'version'"):
            load_policy(write_policy(tmp_path, duplicated))

    def test_duplicate_account_key(self, tmp_path):
        account = json.dumps(valid_document()["accounts"]["example"])
        text = (
            f'{{"version": 1, "identity": "browser", "vault": "{VAULT}", '
            f'"accounts": {{"example": {account}, "example": {account}}}}}'
        )
        with pytest.raises(LoginPolicyError, match="duplicate key: 'example'"):
            load_policy(write_policy(tmp_path, text))


class TestStructuralFailures:
    @pytest.mark.parametrize(
        "mutation, complaint",
        [
            (lambda d: d.pop("version"), "missing field"),
            (lambda d: d.update(version=2), "version"),
            (lambda d: d.pop("identity"), "missing field"),
            (lambda d: d.update(identity="bad name"), "identity"),
            (lambda d: d.update(identity="-lead"), "identity"),
            (lambda d: d.update(identity="x" * 65), "identity"),
            (lambda d: d.update(vault="NotAUUID"), "vault"),
            (lambda d: d.update(vault="dqss6l4yprxhntxymzjaltchwX"), "vault"),
            (lambda d: d.update(accounts={}), "non-empty"),
            (lambda d: d["accounts"]["example"].pop("item"), "missing field"),
            (lambda d: d["accounts"]["example"].update(item="uppercase"), "item"),
            (lambda d: d["accounts"]["example"].pop("username_selector"), "missing field"),
            (lambda d: d["accounts"]["example"].update(username_selector=""), "selector"),
            (lambda d: d["accounts"]["example"].update(username_selector="a\nb"), "selector"),
            (lambda d: d["accounts"]["example"].update(typo_field="x"), "unknown field"),
            (lambda d: d.update(extra="x"), "unknown field"),
        ],
    )
    def test_each_malformed_shape_is_refused(self, tmp_path, mutation, complaint):
        document = valid_document()
        mutation(document)
        with pytest.raises(LoginPolicyError, match=complaint):
            load_policy(write_policy(tmp_path, document))

    def test_account_count_is_bounded(self, tmp_path):
        document = valid_document()
        base = document["accounts"]["example"]
        for index in range(MAX_ACCOUNTS):
            document["accounts"][f"a{index:03d}"] = dict(base)
        with pytest.raises(LoginPolicyError, match="at most"):
            load_policy(write_policy(tmp_path, document))

    def test_bad_account_alias_is_refused(self, tmp_path):
        document = valid_document()
        document["accounts"]["-bad"] = dict(document["accounts"]["example"])
        with pytest.raises(LoginPolicyError, match="account alias"):
            load_policy(write_policy(tmp_path, document))

    def test_errors_never_print_the_configured_values(self, tmp_path):
        """The offending selector/origin text stays out of the diagnostic."""
        document = valid_document()
        document["accounts"]["example"]["username_selector"] = "SECRET" + "x" * 300
        with pytest.raises(LoginPolicyError, match="selector") as excinfo:
            load_policy(write_policy(tmp_path, document))
        assert "SECRET" not in str(excinfo.value)

        document = valid_document()
        document["accounts"]["example"]["origin"] = "https://SECRET@example.com"
        with pytest.raises(LoginPolicyError, match="origin") as excinfo:
            load_policy(write_policy(tmp_path, document))
        assert "SECRET" not in str(excinfo.value)


class TestOriginCanonicalization:
    @pytest.mark.parametrize(
        "origin, canonical",
        [
            ("https://example.com", "https://example.com"),
            ("https://example.com/", "https://example.com"),
            ("https://example.com:8443", "https://example.com:8443"),
            ("https://example.com:443", "https://example.com"),
            ("https://127.0.0.1:9443", "https://127.0.0.1:9443"),
            ("https://sub.example.com", "https://sub.example.com"),
        ],
    )
    def test_accepted_origins_canonicalize(self, tmp_path, origin, canonical):
        document = valid_document()
        document["accounts"]["example"]["origin"] = origin
        policy = load_policy(write_policy(tmp_path, document))
        assert policy.account("example").origin == canonical

    @pytest.mark.parametrize(
        "origin",
        [
            "http://example.com",
            "https://user@example.com",
            "https://user:pw@example.com",
            "https://example.com/path",
            "https://example.com?q=1",
            "https://example.com#frag",
            "https:///nohost",
            "ftp://example.com",
            "//example.com",
            "https://EXAMPLE.com",
            "https://example.com:0x50",
            "https://example.com:99999",
            "https://[::1]:8443",
            "https://*.example.com",
            "https://",
            "https://..com",
            "https://a..b.com",
            "example.com",
        ],
    )
    def test_refused_origins(self, tmp_path, origin):
        document = valid_document()
        document["accounts"]["example"]["origin"] = origin
        with pytest.raises(LoginPolicyError, match="origin"):
            load_policy(write_policy(tmp_path, document))


class TestPathResolution:
    def test_default_path_lives_in_the_xdg_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert policy_path(None) == tmp_path / "browser-tools" / "logins.json"

    def test_xdg_unset_falls_back_to_home_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert policy_path(None) == tmp_path / ".config" / "browser-tools" / "logins.json"

    def test_config_flag_overrides(self, tmp_path):
        assert policy_path(str(tmp_path / "custom.json")) == tmp_path / "custom.json"


class TestLoadAccount:
    def test_known_alias_returns_policy_and_account(self, tmp_path):
        policy, account = load_account(str(write_policy(tmp_path, valid_document())), "example")
        assert account.alias == "example"
        assert policy.vault == VAULT

    def test_unknown_alias_names_the_configured_ones(self, tmp_path):
        with pytest.raises(LoginPolicyError) as excinfo:
            load_account(str(write_policy(tmp_path, valid_document())), "other")
        assert "'other'" in str(excinfo.value)
        assert "example" in str(excinfo.value)

    def test_malformed_policy_beats_unknown_alias(self, tmp_path):
        path = write_policy(tmp_path, "{")
        with pytest.raises(LoginPolicyError, match="not valid JSON"):
            load_account(str(path), "other")
