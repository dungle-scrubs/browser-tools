"""Enforce the written style rules and the documented opt-in contract for #201."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from browser_tools import lifecycle, onepassword
from browser_tools.core import launcher


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
        (onepassword.OnePasswordProvisionError, ("Args:",)),
        (onepassword._refuse, ("Args:", "Returns:")),
        (onepassword._read_manifest, ("Args:", "Returns:", "Raises:")),
        (onepassword._write_manifest, ("Args:", "Raises:")),
        (onepassword.provision, ("Args:", "Raises:")),
        (lifecycle._check_onepassword_eligibility, ("Args:", "Raises:")),
        (lifecycle._provision_onepassword, ("Args:", "Raises:")),
        # The restored preference helper is this change's one vendored-launcher
        # exception (#201), so its boundary documentation is enforced by the
        # same scoped check rather than a whole-repo docstring sweep.
        (launcher._prepare_preferences, ("Args:", "Raises:")),
        (launcher._reject_non_standard_json_constant, ("Args:", "Raises:")),
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


def test_new_onepassword_test_classes_fully_annotate_public_methods() -> None:
    """Keep the two #201 test classes' public methods fully annotated.

    The repository's type-check command covers only ``src/``, so
    CONTRIBUTING.md's annotation rule is unenforced in tests. The check is
    scoped to the two classes the #201 work added; legacy test methods
    elsewhere keep their shape.
    """
    scope = {
        "test_core_launcher.py": "TestProfilePreferences",
        "test_onepassword_lifecycle.py": "TestAutomaticRegistration",
    }
    problems: list[str] = []
    for filename, class_name in scope.items():
        source = (Path(__file__).parent / filename).read_text()
        tree = ast.parse(source, filename=filename)
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        if not matches:
            problems.append(f"{filename}: class {class_name} not found")
            continue
        methods = [
            node
            for node in matches[0].body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for method in methods:
            if method.name.startswith("_"):
                continue
            unannotated = [
                arg.arg
                for arg in (
                    *method.args.posonlyargs,
                    *method.args.args,
                    *method.args.kwonlyargs,
                )
                if arg.arg not in ("self", "cls") and arg.annotation is None
            ]
            for extra in (method.args.vararg, method.args.kwarg):
                if extra is not None and extra.annotation is None:
                    unannotated.append(extra.arg)
            returns_none = isinstance(method.returns, ast.Constant) and method.returns.value is None
            parts = []
            if unannotated:
                parts.append("missing annotations for " + ", ".join(unannotated))
            if not returns_none:
                parts.append("missing -> None")
            if parts:
                problems.append(f"{filename}::{class_name}.{method.name}: " + "; ".join(parts))
    assert not problems, (
        "CONTRIBUTING.md requires type annotations on all public functions. The "
        "#201 test classes' public methods must annotate every parameter "
        "(pytest.MonkeyPatch, Path, and the fixtures' return types) and use "
        "-> None. Unannotated: " + "; ".join(problems)
    )


def test_the_guide_documents_the_automatic_registration_default() -> None:
    """Pin the automatic-default paragraph and its boundaries (Tests T4).

    Replacing the automatic paragraph with the old opt-in wording survived
    every round-one rules test; this contract kills that documentation
    mutant and keeps the automatic default, its boundaries, and its retry
    behavior stated.
    """
    guide = Path(onepassword.__file__).with_name("GUIDE.txt").read_text()
    title = "1PASSWORD DESKTOP INTEGRATION."
    section = " ".join(guide.split(title, 1)[1].split("Chrome looks for", 1)[0].split())
    for requirement in (
        "brand-new Named Chrome profile on macOS is registered automatically",
        "An existing profile is never touched automatically",
        "Without the flag, other platforms, the Camoufox engine, unnamed "
        "launches, and machines without the official registration keep a plain launch",
        "A present but invalid official registration, including a link whose target is gone",
        "removes the empty new profile directory",
        "unchanged invalid registration is refused again",
        "a repaired one registers",
        "is kept unchanged",
        "provision the existing profile with --1password",
    ):
        assert requirement.lower() in section.lower(), (
            f"bt guide's automatic-registration paragraph must state {requirement!r}. "
            "Keep the automatic default, its boundaries, and its retry behavior "
            "documented."
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
