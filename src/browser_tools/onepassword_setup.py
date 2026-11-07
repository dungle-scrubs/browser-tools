"""Automatic 1Password setup for fresh Chrome sessions and the pin default.

This module owns the #201 policy the provisioner deliberately does not:
which launches get an automatic setup, and the new-profile preference that
pins the stable 1Password extension. The provisioning itself stays in
:mod:`browser_tools.onepassword`, whose signature, origin, bounded-input and
no-overwrite defenses are reused unchanged.

Nothing here executes a browser or a 1Password helper: setup writes files
into one freshly selected directory before Chrome starts. Every failure
carries paths only, never document values.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from typing import TYPE_CHECKING

from . import onepassword
from .lifecycle import LifecycleError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Chrome's per-profile preference document, inside the user-data directory.
DEFAULT_DIR_NAME = "Default"
PREFERENCES_NAME = "Preferences"
PREFERENCES_MODE = 0o600

#: The extension-id shape of every ``chrome-extension://`` origin.
_EXTENSION_ID_PATTERN = re.compile(r"[a-p]{32}")


class SessionSetupError(LifecycleError):
    """An automatic session setup step failed before the browser started.

    The message is path-only: which directory failed and what to do next,
    never a preference document's values.
    """


def stable_extension_id() -> str:
    """Derive the stable 1Password extension id from the validated origin.

    The id is computed from :data:`onepassword.STABLE_EXTENSION_ORIGIN` at call
    time rather than copied, so a corrected constant flows through and a
    transcription error cannot survive.

    Returns:
        The 32-character extension id.

    Raises:
        SessionSetupError: The stable origin constant does not carry a
            well-formed extension id (a programming error, named without paths).
    """
    origin = onepassword.STABLE_EXTENSION_ORIGIN
    extension_id = origin.removeprefix("chrome-extension://").removesuffix("/")
    if not _EXTENSION_ID_PATTERN.fullmatch(extension_id):
        raise SessionSetupError(
            "The stable 1Password extension origin does not name a valid extension id."
        )
    return extension_id


def pin_document() -> dict[str, dict[str, list[str]]]:
    """Return the static preference document that pins the stable extension.

    Returns:
        ``{"extensions": {"pinned_extensions": [<stable id>]}}`` and nothing
        else; the seeded file never carries another key.
    """
    return {"extensions": {"pinned_extensions": [stable_extension_id()]}}


def seed_pin(user_data_dir: Path) -> None:
    """Create the missing Default/Preferences that pins the stable extension.

    The seed is one static document published atomically and exclusively: a
    staging file is written beside the destination and linked into place, so
    the file appears whole or not at all and any entry already at the name -
    a document with a pin choice, an empty list, a symlink, anything - is
    left untouched and never read. No symlink on the path is followed, so a
    redirected Default or Preferences cannot make the seed land outside the
    selected directory. An unresolvable target skips the seed rather than
    writing somewhere it cannot verify; Chrome's own preference preparation
    still runs after this and preserves the extensions field.

    Args:
        user_data_dir: The session or profile directory Chrome will use.

    Raises:
        SessionSetupError: Publishing the seed failed after the target name
            was verified absent. The message names the directory and the
            operating system's reason only, never document values.
    """
    with contextlib.ExitStack() as stack:
        try:
            profile_fd = os.open(user_data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return
        stack.callback(os.close, profile_fd)
        # An existing entry of any other kind is resolved below; a directory
        # that cannot be created or held skips the seed.
        with contextlib.suppress(OSError):
            os.mkdir(DEFAULT_DIR_NAME, mode=0o700, dir_fd=profile_fd)
        try:
            default_fd = os.open(
                DEFAULT_DIR_NAME,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=profile_fd,
            )
        except OSError:
            return
        stack.callback(os.close, default_fd)
        try:
            os.stat(PREFERENCES_NAME, dir_fd=default_fd, follow_symlinks=False)
        except OSError:
            # Only a name that is verifiably absent may be seeded; anything
            # unreadable is treated as present so it is never overwritten.
            pass
        else:
            return

        staging = f".{PREFERENCES_NAME}-{uuid.uuid4().hex}"
        try:
            fd = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                PREFERENCES_MODE,
                dir_fd=default_fd,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    os.fchmod(handle.fileno(), PREFERENCES_MODE)
                    json.dump(pin_document(), handle, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise SessionSetupError(
                    f"Cannot seed the 1Password extension pin in {user_data_dir}: "
                    f"{exc.strerror or type(exc).__name__}."
                ) from None
            try:
                os.link(staging, PREFERENCES_NAME, src_dir_fd=default_fd, dst_dir_fd=default_fd)
            except FileExistsError:
                # A concurrent writer published first; its choice stays.
                return
            except OSError as exc:
                raise SessionSetupError(
                    f"Cannot seed the 1Password extension pin in {user_data_dir}: "
                    f"{exc.strerror or type(exc).__name__}."
                ) from None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(staging, dir_fd=default_fd)


def new_profile_initializer() -> Callable[[Path], None]:
    """Build the initializer a newly provisioned Named Profile runs.

    Provisioning has already happened under the profile's launch lock before
    the launcher call; the initializer seeds only the missing pin default into
    the same directory, before Chrome starts. A seeding failure names the
    profile directory and the remedy a named profile can actually use: the
    explicit flag revalidates and re-runs this seed on the retry.

    Returns:
        A callable taking the profile's user-data directory.
    """

    def initialize(user_data_dir: Path) -> None:
        try:
            seed_pin(user_data_dir)
        except SessionSetupError as exc:
            raise SessionSetupError(f"{exc} Relaunch with --1password to retry the setup.") from exc

    return initialize


def temporary_session_initializer() -> Callable[[Path], None]:
    """Build the initializer an anonymous macOS Chrome session runs.

    The initializer provisions the throwaway session's user-data directory
    through the existing validated provisioner and then seeds the pin default,
    both before Chrome starts. A provisioner failure is re-raised as a
    :class:`SessionSetupError` carrying the provisioner's safe reason plus
    retry advice that is valid for an unnamed launch: the failed throwaway
    directory is discarded by the launcher, so relaunching retries the whole
    setup. The explicit flag's remedy is deliberately absent, because
    ``--1password`` requires a named profile and cannot help here.

    Returns:
        A callable taking the freshly allocated session directory.
    """

    def initialize(user_data_dir: Path) -> None:
        try:
            onepassword.provision(user_data_dir)
        except onepassword.OnePasswordProvisionError as exc:
            raise SessionSetupError(
                f"{exc.reason} The failed temporary session is discarded; "
                "launch again to retry after fixing the condition above."
            ) from exc
        seed_pin(user_data_dir)

    return initialize
