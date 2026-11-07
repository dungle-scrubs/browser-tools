"""Enforce the written style rules and the documented opt-in contract for #201."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from browser_tools import lifecycle, onepassword


def test_onepassword_module_respects_the_written_line_limit() -> None:
    """Enable E501 for the new module without changing the legacy exclusions."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "E501",
            "--line-length",
            "100",
            onepassword.__file__,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "onepassword.py breaks CONTRIBUTING.md's 100-character line limit. "
        "Run ruff format and split any remaining long literals.\n" + result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    ("function", "sections"),
    [
        (onepassword.source_manifest_path, ("Returns:",)),
        (onepassword._read_manifest, ("Args:", "Returns:", "Raises:")),
        (onepassword._write_manifest, ("Args:", "Raises:")),
        (onepassword.provision, ("Args:", "Raises:")),
        (lifecycle._check_onepassword_eligibility, ("Args:", "Raises:")),
        (lifecycle._provision_onepassword, ("Args:", "Raises:")),
    ],
)
def test_integration_boundaries_have_google_style_sections(function, sections) -> None:
    """Keep the nontrivial boundary documentation in CONTRIBUTING's format."""
    doc = inspect.getdoc(function) or ""
    for section in sections:
        assert section in doc.splitlines(), (
            f"{function.__module__}.{function.__name__} is missing {section} "
            "from its Google-style docstring (CONTRIBUTING.md). "
            "Document the boundary's arguments, result, and failures."
        )


def test_the_guide_documents_the_new_registration_contract() -> None:
    guide = Path(onepassword.__file__).with_name("GUIDE.txt").read_text()
    title = "1PASSWORD DESKTOP INTEGRATION."
    assert title in guide, "Document --1password's policy and limits in bt guide."
    section = " ".join(guide.split(title, 1)[1].split("APPROVED LOGINS", 1)[0].split())
    for requirement in (
        "bt launch --profile shopify-admin --1password",
        "Opt in",
        "macOS",
        "Chrome engine",
        "official stable 1Password Chrome extension",
        "user-data directory",
        "com.1password.1password.json",
        "64 KiB",
        "code-signature verification",
        "team 2BUA8C4S2C",
        "Registrations persist",
        "Repeat --1password",
        "No existing destination is overwritten",
        "changed helper path",
        "changed description",
        "policy update",
        "not a connection",
        "not validate or remove anything",
        "source survives",
        "External endpoints",
        "Camoufox",
        "selected Chrome build",
        "Keep the existing registration",
        "remove it yourself to re-provision",
        "no credentials",
        "browser trust lists",
        "direct links",
        "source's origin list",
        "exit 2",
        "exit 1",
    ):
        assert requirement.lower() in section.lower(), (
            f"bt guide's 1Password section must explain {requirement!r}. "
            "Keep the opt-in policy and its limits documented."
        )
