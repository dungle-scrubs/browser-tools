"""Unit coverage for the 1Password provisioning module (#201).

Every test runs against tmp directories. The official source registration is
monkeypatched to a temporary path (autouse, so no test can read the
developer's own ``~/Library``), and codesign is replaced at the subprocess
boundary -- its exact requirement is asserted, never assumed, and its output
is proven to stay out of every diagnostic. No profile here is a real one.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

from browser_tools import lifecycle, onepassword
from browser_tools.onepassword import OnePasswordProvisionError

STABLE = onepassword.STABLE_EXTENSION_ORIGIN
#: A second, syntactically valid origin a vendor manifest may carry.
OTHER_ORIGIN = "chrome-extension://" + "b" * 32 + "/"

HOSTS = "NativeMessagingHosts"
DEST_NAME = "com.1password.1password.json"


class CodesignDouble:
    """Stands in at the subprocess boundary and records every invocation.

    Output carries a marker string, so a test can prove it never reaches a
    diagnostic. Failures are configured per test, not baked in.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.exit_code = 0
        self.timeout = False
        self.spawn_error: OSError | None = None
        self.output = b"OUTPUT-MARKER codesign said something"

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(args), kwargs))
        if self.timeout:
            timeout = cast("float", kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd="codesign", timeout=timeout)
        if self.spawn_error is not None:
            raise self.spawn_error
        return subprocess.CompletedProcess(args, self.exit_code, b"", self.output)

    @property
    def marker(self) -> str:
        return self.output.decode()

    @property
    def argv(self) -> list[str]:
        assert self.calls, "codesign was never invoked"
        return self.calls[0][0]

    @property
    def kwargs(self) -> dict[str, object]:
        assert self.calls, "codesign was never invoked"
        return dict(self.calls[0][1])


@pytest.fixture(autouse=True)
def _never_read_the_real_registration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the source path into an empty tmp tree for every test."""
    monkeypatch.setattr(
        onepassword,
        "SOURCE_MANIFEST",
        tmp_path / "absent" / HOSTS / DEST_NAME,
    )


@pytest.fixture(autouse=True)
def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture(autouse=True)
def codesign(monkeypatch: pytest.MonkeyPatch) -> CodesignDouble:
    """Replace codesign at the subprocess boundary; succeed by default."""
    double = CodesignDouble()
    monkeypatch.setattr(onepassword, "subprocess", types.SimpleNamespace(run=double))
    return double


@pytest.fixture
def helper_binary(tmp_path: Path) -> Path:
    helper = tmp_path / "app" / "1Password-BrowserSupport"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    return helper


@pytest.fixture
def write_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[dict[str, object]], Path]:
    def _write(document: dict[str, object]) -> Path:
        hosts = tmp_path / "official-chrome" / HOSTS
        hosts.mkdir(parents=True, exist_ok=True)
        path = hosts / DEST_NAME
        path.write_text(json.dumps(document))
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", path)
        return path

    return _write


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "profiles" / "work"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def destination(profile_dir: Path) -> Path:
    return profile_dir / HOSTS / DEST_NAME


def source_document(helper: Path, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "name": "com.1password.1password",
        "description": "1Password native messaging host",
        "path": str(helper),
        "type": "stdio",
        "allowed_origins": [STABLE, OTHER_ORIGIN],
    }
    document.update(overrides)
    return document


def canonical_manifest(helper: Path) -> dict[str, object]:
    return {
        "name": "com.1password.1password",
        "description": "1Password native messaging host",
        "path": str(helper),
        "type": "stdio",
        "allowed_origins": [STABLE],
    }


@pytest.fixture
def installed(write_source, helper_binary: Path) -> Path:
    """A complete, valid official installation; provisioning succeeds."""
    write_source(source_document(helper_binary))
    return helper_binary


def provision_fails(profile_dir: Path) -> str:
    with pytest.raises(OnePasswordProvisionError) as excinfo:
        onepassword.provision(profile_dir)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------


class TestSourceValidation:
    def test_missing_installation_names_the_remedy(self, profile_dir: Path):
        message = provision_fails(profile_dir)
        assert "No official 1Password registration" in message
        assert "Install the 1Password desktop app" in message
        assert "without --1password" in message
        # A source failure writes nothing, not even the hosts directory.
        assert not (profile_dir / HOSTS).exists()

    def test_symlinked_source_is_rejected(
        self, monkeypatch, write_source, profile_dir: Path, helper_binary: Path
    ):
        real = write_source(source_document(helper_binary))
        link = real.parent / "linked.json"
        link.symlink_to(real)
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", link)
        assert "regular file" in provision_fails(profile_dir)

    def test_directory_source_is_rejected(self, monkeypatch, tmp_path, profile_dir: Path):
        directory = tmp_path / "not-a-file"
        directory.mkdir()
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", directory)
        assert "regular file" in provision_fails(profile_dir)

    def test_oversized_source_is_rejected(self, write_source, profile_dir: Path, helper_binary):
        padded = "x" * onepassword.MAX_MANIFEST_BYTES
        write_source(source_document(helper_binary, description=padded))
        message = provision_fails(profile_dir)
        assert "larger than" in message
        assert str(onepassword.MAX_MANIFEST_BYTES) in message

    def test_source_read_requests_only_the_bounded_payload(
        self, installed, profile_dir: Path, monkeypatch
    ):
        real_fdopen = os.fdopen
        requested_sizes = []

        class BoundedReader:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def fileno(self):
                return self.handle.fileno()

            def read(self, size=-1):
                requested_sizes.append(size)
                assert 0 <= size <= 65537, "Read only the 64 KiB limit plus one overflow byte"
                return self.handle.read(size)

        def guarded_fdopen(fd, mode, *args, **kwargs):
            handle = real_fdopen(fd, mode, *args, **kwargs)
            return BoundedReader(handle) if mode == "rb" else handle

        monkeypatch.setattr(os, "fdopen", guarded_fdopen)
        onepassword.provision(profile_dir)
        assert requested_sizes == [65537]

    def test_nul_in_helper_path_is_refused_before_stat(
        self, write_source, profile_dir: Path, helper_binary
    ):
        write_source(source_document(helper_binary, path="/Applications/bad\u0000helper"))
        assert "absolute helper path" in provision_fails(profile_dir)

    def test_malformed_json_is_rejected(self, write_source, profile_dir: Path):
        path = write_source(source_document(Path("/bin/true")))
        path.write_text("{not json")
        assert "not valid JSON" in provision_fails(profile_dir)

    def test_non_object_json_is_rejected(self, write_source, profile_dir: Path):
        path = write_source(source_document(Path("/bin/true")))
        path.write_text("[1, 2]")
        assert "not a JSON object" in provision_fails(profile_dir)

    def test_unknown_field_is_rejected(self, write_source, profile_dir: Path, helper_binary):
        write_source(source_document(helper_binary, unexpected_field=[STABLE]))
        assert "unknown field(s): unexpected_field" in provision_fails(profile_dir)

    def test_missing_field_is_rejected(self, write_source, profile_dir: Path, helper_binary):
        document = source_document(helper_binary)
        del document["description"]
        write_source(document)
        assert "missing field(s): description" in provision_fails(profile_dir)

    def test_wrong_host_name_is_rejected(self, write_source, profile_dir: Path, helper_binary):
        write_source(source_document(helper_binary, name="com.other.host"))
        assert "must name host 'com.1password.1password'" in provision_fails(profile_dir)

    def test_wrong_type_is_rejected(self, write_source, profile_dir: Path, helper_binary):
        write_source(source_document(helper_binary, type="pwdt"))
        assert "'stdio'" in provision_fails(profile_dir)

    def test_non_string_description_is_rejected(self, write_source, profile_dir, helper_binary):
        write_source(source_document(helper_binary, description=5))
        assert "non-string description" in provision_fails(profile_dir)

    def test_relative_helper_path_is_rejected(self, write_source, profile_dir, helper_binary):
        write_source(source_document(helper_binary, path="1Password-BrowserSupport"))
        assert "absolute helper path" in provision_fails(profile_dir)

    def test_non_string_helper_path_is_rejected(self, write_source, profile_dir, helper_binary):
        write_source(source_document(helper_binary, path=7))
        assert "absolute helper path" in provision_fails(profile_dir)

    def test_empty_allowed_origins_is_rejected(self, write_source, profile_dir, helper_binary):
        write_source(source_document(helper_binary, allowed_origins=[]))
        assert "no non-empty allowed_origins" in provision_fails(profile_dir)

    def test_malformed_origin_is_rejected(self, write_source, profile_dir: Path, helper_binary):
        write_source(
            source_document(helper_binary, allowed_origins=[STABLE, "https://example.com/"])
        )
        assert "malformed extension origin" in provision_fails(profile_dir)

    def test_missing_stable_origin_is_rejected(self, write_source, profile_dir, helper_binary):
        write_source(source_document(helper_binary, allowed_origins=[OTHER_ORIGIN]))
        assert "stable 1Password Chrome extension origin" in provision_fails(profile_dir)

    def test_validation_precedes_any_destination_decision(
        self, profile_dir: Path, destination: Path
    ):
        """A conflicting destination AND no source: the source failure is the
        one raised, and the destination is untouched."""
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        destination.write_text("{}")
        message = provision_fails(profile_dir)
        assert "No official 1Password registration" in message
        assert destination.read_text() == "{}"


# ---------------------------------------------------------------------------
# Helper validation and the codesign boundary
# ---------------------------------------------------------------------------


class TestHelperValidation:
    def test_missing_helper_is_rejected(self, write_source, profile_dir: Path):
        write_source(source_document(Path("/nonexistent/helper")))
        message = provision_fails(profile_dir)
        assert "/nonexistent/helper" in message
        assert "does not exist" in message

    def test_helper_inspection_permission_failure_keeps_its_actual_cause(
        self, installed, profile_dir: Path, monkeypatch
    ):
        real_stat = os.stat

        def denied_stat(path, *args, **kwargs):
            if str(path) == str(installed):
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", denied_stat)
        message = provision_fails(profile_dir)
        assert "Cannot inspect the 1Password helper" in message
        assert "Permission denied" in message
        assert "does not exist" not in message

    def test_nonexecutable_helper_is_rejected(
        self, write_source, profile_dir: Path, helper_binary: Path
    ):
        helper_binary.chmod(0o644)
        write_source(source_document(helper_binary))
        assert "not an executable file" in provision_fails(profile_dir)

    def test_helper_directory_is_rejected(self, write_source, profile_dir: Path, tmp_path):
        directory = tmp_path / "helper-dir"
        directory.mkdir()
        directory.chmod(0o755)
        write_source(source_document(directory))
        assert "not an executable file" in provision_fails(profile_dir)


class TestCodesignBoundary:
    def test_the_exact_trust_requirement_is_passed(
        self, installed, profile_dir: Path, destination: Path, codesign: CodesignDouble
    ):
        onepassword.provision(profile_dir)
        assert destination.exists()  # validation really ran and passed
        assert codesign.argv == [
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            "-R",
            "=" + onepassword.CODESIGN_REQUIREMENT,
            str(installed),
        ]
        assert onepassword.CODESIGN_REQUIREMENT == (
            'anchor apple generic and identifier "com.1password.browser-support" '
            'and certificate leaf[subject.OU] = "2BUA8C4S2C"'
        )
        assert codesign.kwargs["capture_output"] is True
        assert codesign.kwargs["timeout"] == onepassword.CODESIGN_TIMEOUT_SECONDS
        assert codesign.kwargs["check"] is False

    def test_unsigned_helper_is_rejected_without_its_output(
        self, installed, profile_dir: Path, codesign: CodesignDouble
    ):
        codesign.exit_code = 1
        message = provision_fails(profile_dir)
        assert "did not pass code-signature verification" in message
        assert "update browser-tools" in message
        assert "signing identity changed" in message
        assert "without --1password" in message
        assert codesign.marker not in message
        assert not (profile_dir / HOSTS).exists()

    def test_codesign_timeout_is_rejected(
        self, installed, profile_dir: Path, codesign: CodesignDouble
    ):
        codesign.timeout = True
        message = provision_fails(profile_dir)
        assert "timed out" in message
        assert codesign.marker not in message

    def test_codesign_spawn_failure_is_a_fixed_diagnostic(
        self, installed, profile_dir: Path, codesign: CodesignDouble
    ):
        codesign.spawn_error = OSError("secret spawn detail")
        message = provision_fails(profile_dir)
        assert "could not be run" in message
        assert "secret spawn detail" not in message
        assert codesign.marker not in message


# ---------------------------------------------------------------------------
# Writing and reusing registrations
# ---------------------------------------------------------------------------


class TestProvisioning:
    def test_new_profile_gets_a_narrowed_regular_manifest(
        self, installed, profile_dir: Path, destination: Path
    ):
        before = onepassword.source_manifest_path().read_bytes()
        onepassword.provision(profile_dir)
        written = json.loads(destination.read_text())
        assert written == canonical_manifest(installed)
        # Narrowed: the vendor source allowed two origins; the copy allows one.
        assert written["allowed_origins"] == [STABLE]
        assert set(written) == {"name", "description", "path", "type", "allowed_origins"}
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert not destination.is_symlink()
        # The source is unchanged, origins included.
        source = json.loads(onepassword.source_manifest_path().read_text())
        assert source["allowed_origins"] == [STABLE, OTHER_ORIGIN]
        assert onepassword.source_manifest_path().read_bytes() == before

    def test_repeat_provisioning_is_exactly_idempotent(
        self, installed, profile_dir: Path, destination: Path
    ):
        onepassword.provision(profile_dir)
        first = destination.stat()
        onepassword.provision(profile_dir)
        second = destination.stat()
        assert (first.st_ino, first.st_mtime_ns) == (second.st_ino, second.st_mtime_ns)
        assert json.loads(destination.read_text()) == canonical_manifest(installed)

    def test_existing_matching_registration_is_accepted_unchanged(
        self, installed, profile_dir: Path, destination: Path
    ):
        # Different formatting and key order, same parsed manifest.
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        reordered = dict(reversed(list(canonical_manifest(installed).items())))
        destination.write_text(json.dumps(reordered, indent=4))
        before = destination.read_bytes()
        onepassword.provision(profile_dir)
        assert destination.read_bytes() == before

    def test_existing_differing_registration_is_a_conflict(
        self, installed, profile_dir: Path, destination: Path
    ):
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        existing = canonical_manifest(installed)
        existing["description"] = "something else"
        destination.write_text(json.dumps(existing))
        before = destination.read_bytes()
        message = provision_fails(profile_dir)
        assert "holds a conflicting registration" in message
        assert "Inspect this file" in message
        assert "differs in fields: description" in message
        assert "may have moved" not in message
        assert "remove it yourself to re-provision" in message
        assert "without --1password" in message
        assert destination.read_bytes() == before

    def test_changed_helper_path_is_a_conflict_naming_the_update(
        self, write_source, helper_binary: Path, profile_dir: Path, destination: Path, tmp_path
    ):
        write_source(source_document(helper_binary))
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        stale = canonical_manifest(helper_binary)
        stale["path"] = str(tmp_path / "old" / "1Password-BrowserSupport")
        destination.write_text(json.dumps(stale))
        message = provision_fails(profile_dir)
        assert "helper binary may have moved in a 1Password update" in message
        assert "resolve the difference and rerun" in message
        assert destination.read_text() == json.dumps(stale)

    def test_existing_unparseable_registration_is_a_conflict(
        self, installed, profile_dir: Path, destination: Path
    ):
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        destination.write_text("not json at all")
        before = destination.read_bytes()
        message = provision_fails(profile_dir)
        assert "holds a conflicting registration" in message
        assert "cannot be read as a bounded JSON registration" in message
        assert "may have moved" not in message
        assert destination.read_bytes() == before

    def test_official_source_link_is_preserved(
        self, installed, profile_dir: Path, destination: Path, codesign: CodesignDouble
    ):
        """The already-working manual repair: a link straight at the official
        registration, validated and kept exactly as the operator made it."""
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        destination.symlink_to(onepassword.source_manifest_path())
        onepassword.provision(profile_dir)
        assert destination.is_symlink()
        assert os.readlink(destination) == str(onepassword.source_manifest_path())
        # The link was accepted after validation, not instead of it.
        assert len(codesign.calls) == 1

    def test_relative_link_under_aliased_root_must_reach_the_actual_source(
        self, installed, tmp_path: Path
    ):
        root = tmp_path / "physical" / "deep" / "profiles"
        real_profile = root / "work"
        (real_profile / HOSTS).mkdir(parents=True)
        alias = tmp_path / "profiles"
        alias.symlink_to(root)
        profile = alias / "work"
        destination = profile / HOSTS / DEST_NAME
        source = onepassword.source_manifest_path()
        relative = os.path.relpath(source, destination.parent)
        # Lexically this spells the source. The kernel follows it from the
        # physical parent, where it reaches this different registration.
        actual_target = Path(os.path.abspath(real_profile / HOSTS / relative))
        actual_target.parent.mkdir(parents=True, exist_ok=True)
        actual_target.write_text("untrusted registration")
        destination.symlink_to(relative)

        assert "conflicting registration" in provision_fails(profile)
        assert destination.is_symlink()
        assert actual_target.read_text() == "untrusted registration"

    def test_relative_source_link_uses_the_physical_parent_under_an_aliased_root(
        self, installed, tmp_path: Path
    ):
        root = tmp_path / "physical" / "deep" / "profiles"
        real_profile = root / "work"
        (real_profile / HOSTS).mkdir(parents=True)
        alias = tmp_path / "profiles"
        alias.symlink_to(root)
        profile = alias / "work"
        destination = profile / HOSTS / DEST_NAME
        relative = os.path.relpath(onepassword.source_manifest_path(), real_profile / HOSTS)
        destination.symlink_to(relative)

        onepassword.provision(profile)

        assert destination.is_symlink()
        assert os.readlink(destination) == relative
        assert destination.stat().st_ino == onepassword.source_manifest_path().stat().st_ino

    def test_relative_official_source_link_is_preserved(
        self, installed, profile_dir: Path, destination: Path
    ):
        destination.parent.mkdir()
        source = onepassword.source_manifest_path()
        target = os.path.relpath(source, destination.parent)
        destination.symlink_to(target)
        onepassword.provision(profile_dir)
        assert destination.is_symlink()
        assert os.readlink(destination) == target

    def test_arbitrary_symlink_is_a_conflict(
        self, installed, profile_dir: Path, destination: Path, tmp_path: Path
    ):
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        elsewhere = tmp_path / "elsewhere" / DEST_NAME
        elsewhere.parent.mkdir()
        elsewhere.write_text("{}")
        destination.symlink_to(elsewhere)
        message = provision_fails(profile_dir)
        assert "holds a conflicting registration" in message
        assert "link does not point directly to the official source" in message
        assert "may have moved" not in message
        assert destination.is_symlink()
        assert elsewhere.read_text() == "{}"

    def test_dangling_symlink_is_a_conflict(self, installed, profile_dir: Path, destination):
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        destination.symlink_to(profile_dir / "gone.json")
        message = provision_fails(profile_dir)
        assert "holds a conflicting registration" in message
        assert destination.is_symlink()

    def test_destination_directory_is_a_conflict(self, installed, profile_dir, destination):
        destination.mkdir(parents=True)
        message = provision_fails(profile_dir)
        assert "holds a conflicting registration" in message
        assert "destination is not a regular file" in message
        assert "may have moved" not in message
        assert destination.is_dir()

    def test_symlinked_hosts_directory_is_rejected(self, installed, profile_dir, tmp_path):
        outside = tmp_path / "outside-hosts"
        outside.mkdir()
        (profile_dir / HOSTS).symlink_to(outside)
        message = provision_fails(profile_dir)
        assert "not symlinks" in message
        assert (profile_dir / HOSTS).is_symlink()
        assert not (outside / DEST_NAME).exists()

    def test_symlinked_profile_directory_is_rejected(self, installed, tmp_path: Path):
        real = tmp_path / "real-profile"
        real.mkdir()
        linked = tmp_path / "linked-profile"
        linked.symlink_to(real)
        message = provision_fails(linked)
        assert "not symlinks" in message
        assert not (real / HOSTS).exists()

    def test_unrelated_host_file_is_preserved(self, installed, profile_dir: Path, destination):
        hosts = profile_dir / HOSTS
        hosts.mkdir()
        unrelated = hosts / "com.google.chrome.example.json"
        unrelated.write_text('{"name": "com.google.chrome.example"}')
        onepassword.provision(profile_dir)
        assert unrelated.read_text() == '{"name": "com.google.chrome.example"}'

    def test_profile_login_sentinel_is_preserved(self, installed, profile_dir: Path):
        sentinel = profile_dir / "Default" / "Login Data"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"login state")
        onepassword.provision(profile_dir)
        assert sentinel.read_bytes() == b"login state"


# ---------------------------------------------------------------------------
# Platform guard and the profile-delete boundary
# ---------------------------------------------------------------------------


class TestPlatformAndDeletion:
    def test_non_darwin_is_refused(self, installed, profile_dir, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        message = provision_fails(profile_dir)
        assert "macOS only" in message
        assert "Inspect the registration" not in message
        assert not (profile_dir / HOSTS).exists()

    def test_profile_deletion_leaves_the_external_registration(
        self, installed, monkeypatch, tmp_path: Path, profile_dir: Path
    ):
        """`profile delete` removes the profile's own registration and never
        follows anything out of it: the everyday-Chrome registration this
        provisioning read from survives untouched (#201).
        """
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "profiles"))
        onepassword.provision(profile_dir)
        source_path = onepassword.source_manifest_path()
        source_before = source_path.read_bytes()

        result = lifecycle.profile_delete("work", registry_path=str(tmp_path / "registry.json"))

        assert result["deleted"] == "work"
        assert not profile_dir.exists()
        assert source_path.read_bytes() == source_before

    def test_profile_deletion_does_not_follow_the_existing_source_link(
        self, installed, monkeypatch, tmp_path: Path, profile_dir: Path, destination: Path
    ):
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "profiles"))
        destination.parent.mkdir()
        source = onepassword.source_manifest_path()
        before = source.read_bytes()
        destination.symlink_to(source)
        onepassword.provision(profile_dir)

        lifecycle.profile_delete("work", registry_path=str(tmp_path / "registry.json"))

        assert not profile_dir.exists()
        assert source.read_bytes() == before


class TestPublicationFailures:
    def test_publication_failure_leaves_no_partial_registration(
        self, installed, profile_dir: Path, destination: Path, monkeypatch
    ):
        def fail_link(*args, **kwargs):
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(os, "link", fail_link)
        message = provision_fails(profile_dir)
        assert "Cannot provision" in message
        assert "Permission denied" in message
        assert "not symlinks" not in message
        assert not destination.exists()
        assert list(destination.parent.iterdir()) == []

    def test_a_registration_appearing_during_publication_is_never_replaced(
        self, installed, profile_dir: Path, destination: Path, monkeypatch
    ):
        real_link = os.link

        def concurrent_link(*args, **kwargs):
            destination.write_text("user-managed concurrent registration")
            return real_link(*args, **kwargs)

        monkeypatch.setattr(os, "link", concurrent_link)
        message = provision_fails(profile_dir)
        assert "conflicting registration" in message
        assert destination.read_text() == "user-managed concurrent registration"
        assert list(destination.parent.iterdir()) == [destination]

    def test_special_file_source_is_refused_without_blocking(
        self, profile_dir: Path, tmp_path: Path, monkeypatch
    ):
        fifo = tmp_path / "source.fifo"
        os.mkfifo(fifo)
        monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", fifo)
        assert "not a regular file" in provision_fails(profile_dir)
        assert not (profile_dir / HOSTS).exists()
