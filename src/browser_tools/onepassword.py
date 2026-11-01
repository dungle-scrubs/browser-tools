"""Opt-in registration of the signed macOS 1Password helper in a Named Profile.

New manifests grant only the stable extension access. Existing registrations
are never replaced, including the official-source symlink used by older manual
repairs. Provisioning does not establish that the extension is connected.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

HOST_NAME = "com.1password.1password"
STABLE_EXTENSION_ORIGIN = "chrome-extension://aeblfdkhhhdcdjpifhhbdiojplfjncoa/"
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}/")
SOURCE_MANIFEST = Path(
    "~/Library/Application Support/Google/Chrome/NativeMessagingHosts/" + HOST_NAME + ".json"
)
# Observed on the official 1Password 8.12.42 helper. A changed identity requires
# a policy update, rather than trusting whatever executable a manifest names.
CODESIGN_REQUIREMENT = (
    'anchor apple generic and identifier "com.1password.browser-support" '
    'and certificate leaf[subject.OU] = "2BUA8C4S2C"'
)
CODESIGN_BINARY = "/usr/bin/codesign"
CODESIGN_TIMEOUT_SECONDS = 10.0
MAX_MANIFEST_BYTES = 64 * 1024
MANIFEST_MODE = 0o600
HOSTS_DIR_NAME = "NativeMessagingHosts"
_ALLOWED_FIELDS = frozenset({"name", "description", "path", "type", "allowed_origins"})


class OnePasswordProvisionError(Exception):
    """A registration could not be safely provisioned."""


@dataclass(frozen=True)
class _ValidatedSource:
    description: str
    helper_path: str


def source_manifest_path() -> Path:
    """Return the single supported source registration for this user.

    Returns:
        The absolute path in everyday Chrome's user-data directory.
    """
    return SOURCE_MANIFEST.expanduser()


def _refuse(message: str) -> OnePasswordProvisionError:
    return OnePasswordProvisionError(message + " Launch without --1password to skip provisioning.")


def _read_manifest(path: Path, *, dir_fd: int | None = None) -> dict[str, Any]:
    """Read a bounded regular file, refusing links and special files.

    Args:
        path: The manifest path, relative to dir_fd when provided.
        dir_fd: An open parent directory, or None for ordinary path lookup.

    Returns:
        The parsed manifest object.

    Raises:
        OnePasswordProvisionError: The file is not bounded regular JSON object data.
        OSError: Opening or reading the manifest failed.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _refuse(f"{path} is not a regular file.")
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise _refuse(f"{path} is larger than {MAX_MANIFEST_BYTES} bytes.")
    try:
        document: object = json.loads(data)
    except (ValueError, RecursionError):
        raise _refuse(f"{path} is not valid JSON.") from None
    if not isinstance(document, dict):
        raise _refuse(f"{path} is not a JSON object.")
    return document


def _validated_source(source: Path) -> _ValidatedSource:
    try:
        document = _read_manifest(source)
    except FileNotFoundError:
        raise _refuse(
            f"No official 1Password registration was found at {source}. "
            "Install the 1Password desktop app and its Chrome extension."
        ) from None
    except OSError:
        raise _refuse(f"{source} could not be read as a regular file.") from None
    unknown = sorted(set(document) - _ALLOWED_FIELDS)
    missing = sorted(_ALLOWED_FIELDS - set(document))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown field(s): {', '.join(unknown)}")
        if missing:
            details.append(f"missing field(s): {', '.join(missing)}")
        raise _refuse(f"{source} does not match the expected schema ({'; '.join(details)}).")
    if document["name"] != HOST_NAME:
        raise _refuse(f"{source} must name host {HOST_NAME!r}.")
    if document["type"] != "stdio":
        raise _refuse(f"{source} must have type 'stdio'.")
    description, helper_path, origins = (
        document["description"],
        document["path"],
        document["allowed_origins"],
    )
    if not isinstance(description, str):
        raise _refuse(f"{source} has a non-string description.")
    if not isinstance(helper_path, str) or not os.path.isabs(helper_path) or "\0" in helper_path:
        raise _refuse(f"{source} does not name an absolute helper path.")
    if not isinstance(origins, list) or not origins:
        raise _refuse(f"{source} has no non-empty allowed_origins list.")
    if any(
        not isinstance(origin, str) or not _EXTENSION_ORIGIN.fullmatch(origin) for origin in origins
    ):
        raise _refuse(f"{source} allows a malformed extension origin.")
    if STABLE_EXTENSION_ORIGIN not in origins:
        raise _refuse(f"{source} does not allow the stable 1Password Chrome extension origin.")
    return _ValidatedSource(description, helper_path)


def _validate_helper(helper_path: str) -> None:
    try:
        info = os.stat(helper_path)
    except FileNotFoundError:
        raise _refuse(
            f"The 1Password helper at {helper_path} does not exist. Reinstall 1Password."
        ) from None
    except OSError as exc:
        raise _refuse(
            f"Cannot inspect the 1Password helper at {helper_path}: {exc.strerror}."
        ) from None
    if not stat.S_ISREG(info.st_mode) or not os.access(helper_path, os.X_OK):
        raise _refuse(f"The 1Password helper at {helper_path} is not an executable file.")
    try:
        result = subprocess.run(
            [
                CODESIGN_BINARY,
                "--verify",
                "--strict",
                "-R",
                "=" + CODESIGN_REQUIREMENT,
                helper_path,
            ],
            capture_output=True,
            timeout=CODESIGN_TIMEOUT_SECONDS,
            check=False,
        )
    except TimeoutExpired:
        raise _refuse(
            "Verifying the 1Password helper timed out. Retry after checking the app."
        ) from None
    except OSError:
        raise _refuse(
            f"{CODESIGN_BINARY} could not be run to verify the 1Password helper."
        ) from None
    if result.returncode != 0:
        raise _refuse(
            "The 1Password helper did not pass code-signature verification. "
            "Repair the installation, or update browser-tools if 1Password's "
            "signing identity changed."
        )


def _accept_existing(
    hosts_fd: int, filename: Path, destination: Path, source: Path, canonical: dict[str, Any]
) -> None:
    info = os.stat(filename, dir_fd=hosts_fd, follow_symlinks=False)
    reason = "the destination is not a regular file"
    if stat.S_ISLNK(info.st_mode):
        target = Path(os.readlink(filename, dir_fd=hosts_fd))
        # Accept only the direct official-source link, without reading any
        # arbitrary destination chosen by a user-managed registration.
        if not target.is_absolute():
            target = destination.parent.resolve() / target
        direct_target = target.parent.resolve() / target.name
        direct_source = source.parent.resolve() / source.name
        if direct_target == direct_source:
            try:
                actual = os.stat(filename, dir_fd=hosts_fd)
                official = os.stat(source)
            except OSError:
                pass
            else:
                if (actual.st_dev, actual.st_ino) == (official.st_dev, official.st_ino):
                    return
        reason = "the link does not point directly to the official source"
    elif stat.S_ISREG(info.st_mode):
        try:
            existing = _read_manifest(filename, dir_fd=hosts_fd)
        except (OSError, OnePasswordProvisionError):
            reason = "the file cannot be read as a bounded JSON registration"
        else:
            if existing == canonical:
                return
            differing = sorted(
                key
                for key in set(existing) | set(canonical)
                if key not in existing or key not in canonical or existing[key] != canonical[key]
            )
            reason = f"the file differs in fields: {', '.join(differing)}"
            if existing.get("path") != canonical["path"]:
                reason += "; the helper binary may have moved in a 1Password update"
    raise _refuse(
        f"{destination} holds a conflicting registration: {reason}. Nothing was replaced. "
        "Inspect this file, including any registration browser-tools previously created. "
        "Keep it, or remove it yourself to re-provision; resolve the difference and rerun."
    )


def _write_manifest(hosts_fd: int, filename: Path, canonical: dict[str, Any]) -> None:
    """Publish a complete manifest without replacing an existing registration.

    Args:
        hosts_fd: The checked native-host directory descriptor.
        filename: The registration filename, relative to hosts_fd.
        canonical: The validated registration to publish.

    Raises:
        FileExistsError: A destination already exists and must be checked separately.
        OSError: Staging or publishing the registration failed.
    """
    staging = f".1password-{uuid.uuid4().hex}.json"
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, MANIFEST_MODE, dir_fd=hosts_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), MANIFEST_MODE)
            handle.write(json.dumps(canonical, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # link() publishes atomically and fails if any destination exists.
        os.link(staging, filename, src_dir_fd=hosts_fd, dst_dir_fd=hosts_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staging, dir_fd=hosts_fd)


def provision(user_data_dir: Path) -> None:
    """Provision one existing Named Profile on macOS, preserving existing files.

    Args:
        user_data_dir: A real Named Profile directory, with its launch lock held.

    Raises:
        OnePasswordProvisionError: Installation, signature, or destination checks fail.
    """
    if sys.platform != "darwin":
        raise _refuse("--1password is supported on macOS only.")
    source = source_manifest_path()
    try:
        # Keep directory handles through publication: replacing a checked path
        # with a symlink cannot redirect a subsequent write to another profile.
        profile_fd = os.open(user_data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        with contextlib.ExitStack() as stack:
            stack.callback(os.close, profile_fd)
            validated = _validated_source(source)
            _validate_helper(validated.helper_path)
            canonical = {
                "name": HOST_NAME,
                "description": validated.description,
                "path": validated.helper_path,
                "type": "stdio",
                "allowed_origins": [STABLE_EXTENSION_ORIGIN],
            }
            with contextlib.suppress(FileExistsError):
                os.mkdir(HOSTS_DIR_NAME, mode=0o700, dir_fd=profile_fd)
            hosts_fd = os.open(
                HOSTS_DIR_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=profile_fd
            )
            stack.callback(os.close, hosts_fd)
            filename = Path(f"{HOST_NAME}.json")
            destination = user_data_dir / HOSTS_DIR_NAME / filename
            try:
                os.stat(filename, dir_fd=hosts_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    _write_manifest(hosts_fd, filename, canonical)
                    return
                except FileExistsError:
                    pass
            _accept_existing(hosts_fd, filename, destination, source, canonical)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            detail = "The profile and NativeMessagingHosts must be real directories, not symlinks."
        else:
            detail = f"Filesystem operation failed: {exc.strerror or type(exc).__name__}."
        raise _refuse(f"Cannot provision {user_data_dir}. {detail}") from None
