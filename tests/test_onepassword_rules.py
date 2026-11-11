"""Enforce the written style rules and the documented opt-in contract for #201."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from browser_tools import lifecycle, onepassword, onepassword_setup, session_initialization
from browser_tools.core import launcher


def test_module_scoped_fixtures_already_see_the_absent_synthetic_source(
    official_source_at_module_scope: Path,
) -> None:
    """Prove the session-level source isolation precedes module-scope setup.

    ``curated_browser`` is module-scoped and set up before any
    function-scoped fixture of its module, so the pre-683749f function-scoped
    patch could not stop a module-scoped browser launch from reading the
    developer's real registration. The module-scoped witness fixture captured
    the path before any function-scoped fixture of this module ran; it must
    already be the session's absent synthetic source. The assertion never
    stats or reads anything under the developer's home.
    """
    assert official_source_at_module_scope == onepassword.source_manifest_path()
    assert "Library/Application Support" not in str(official_source_at_module_scope)
    assert not official_source_at_module_scope.exists()


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
        (lifecycle._temporary_setup_initializer, ("Args:", "Returns:")),
        # The restored preference helper is this change's one vendored-launcher
        # exception (#201), so its boundary documentation is enforced by the
        # same scoped check rather than a whole-repo docstring sweep. The
        # temporary-setup module and the extracted session-initialization
        # marker protocol follow the same rule for the same reason.
        (launcher._prepare_preferences, ("Args:", "Raises:")),
        (launcher._reject_non_standard_json_constant, ("Args:", "Raises:")),
        (launcher._parse_finite_json_float, ("Args:", "Returns:", "Raises:")),
        (session_initialization.publish_initialization_marker, ("Args:", "Returns:")),
        (session_initialization.release_initialization_marker, ("Args:",)),
        (session_initialization.initialization_marker_holds, ("Args:", "Returns:")),
        (session_initialization.session_root_mutex_path, ("Args:", "Returns:")),
        (session_initialization.acquire_session_root_mutex, ("Args:", "Returns:", "Raises:")),
        (session_initialization.release_session_root_mutex, ("Args:",)),
        (session_initialization.session_root_exclusion, ("Args:", "Yields:", "Raises:")),
        (onepassword_setup.stable_extension_id, ("Returns:", "Raises:")),
        (onepassword_setup.pin_document, ("Returns:",)),
        (onepassword_setup.seed_pin, ("Args:", "Raises:")),
        (onepassword_setup.new_profile_initializer, ("Returns:",)),
        (onepassword_setup.temporary_session_initializer, ("Returns:",)),
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


_SETUP_LAYERING_SCRIPT = (
    "import sys; import browser_tools.onepassword_setup as setup; "
    "assert 'browser_tools.lifecycle' not in sys.modules, ("
    "'onepassword_setup imports the lifecycle layer above it; own the error "
    "contract in the setup module and translate it at the lifecycle "
    "boundary'); "
    "assert issubclass(setup.SessionSetupError, RuntimeError), ("
    "'SessionSetupError must stay a RuntimeError subtype so the lifecycle "
    "launch boundary translates it to LifecycleError')"
)


def _lifecycle_imports(source: str) -> list[str]:
    """Find every import of ``browser_tools.lifecycle`` in a package module.

    Args:
        source: The source of a module directly inside ``browser_tools``.

    Returns:
        ``line <n>`` for each ``import`` or ``from`` statement, at any nesting
        depth, that imports the lifecycle module or a name from it.
    """
    target = "browser_tools.lifecycle"
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hit = any(
                alias.name == target or alias.name.startswith(f"{target}.")
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
            elif node.level == 1:
                module = "browser_tools" + (f".{node.module}" if node.module else "")
            else:
                continue
            hit = (
                module == target
                or module.startswith(f"{target}.")
                or (module == "browser_tools" and any(a.name == "lifecycle" for a in node.names))
            )
        else:
            continue
        if hit:
            found.append(f"line {node.lineno}")
    return found


def test_the_setup_module_owns_its_error_below_the_lifecycle_layer() -> None:
    """The setup module never imports the lifecycle layer above it.

    CONTRIBUTING.md's layering rule says there are no circular imports.
    ``lifecycle`` may import ``onepassword_setup`` lazily at its adapter
    seams; the reverse edge - the setup module importing
    ``lifecycle.LifecycleError`` to subclass it - closed the cycle (round-4
    Quality hard finding at ``4484568``). Two checks cover the edge. The
    source scan refuses an import of ``lifecycle`` anywhere in the module,
    including inside a function body, which runs only when that function is
    called (#215). The fresh-interpreter import refuses any ``lifecycle``
    module in the loaded set, which also catches a module-level import made
    through another module. Both are scoped to this one module's imports
    rather than a whole-repository cycle scanner. The setup-owned error must
    stay a ``RuntimeError`` subtype: the lifecycle launch boundary
    translates that shape to ``LifecycleError`` and the CLI's exit 1.
    """
    source = Path(inspect.getfile(onepassword_setup)).read_text(encoding="utf-8")
    found = _lifecycle_imports(source)
    assert not found, (
        "onepassword_setup imports the lifecycle layer above it at "
        + ", ".join(found)
        + " (CONTRIBUTING.md: no circular imports). Own the error contract in "
        "the setup module and translate it at the lifecycle boundary."
    )
    result = subprocess.run(
        [sys.executable, "-c", _SETUP_LAYERING_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "onepassword_setup broke the layering contract (CONTRIBUTING.md: no "
        "circular imports; the error is owned by the setup module and "
        "translated at the lifecycle boundary).\n" + result.stderr
    )


#: The three states a function's return annotation can be in for the
#: annotation scanners below: absent, present but the constant ``None``, and
#: present and naming a type. Each guarded boundary reads the states
#: differently, so the scanner reports the state and the test judges it.
_RETURN_MISSING = "missing"
_RETURN_NONE = "none"
_RETURN_ANNOTATED = "annotated"


def _annotation_report(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    skip_receiver: bool = False,
) -> tuple[list[str], str]:
    """Report a function node's unannotated parameters and return annotation.

    Args:
        node: The function definition to inspect.
        skip_receiver: Whether a ``self`` or ``cls`` receiver is exempt from
            the annotation requirement, as it is for a method's receiver.

    Returns:
        The unannotated parameter names in declaration order, and the return
        annotation's state: ``"missing"`` when there is no return annotation,
        ``"none"`` when it is the constant ``None``, and ``"annotated"`` when
        it names a type.
    """
    unannotated = [
        arg.arg
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        if arg.annotation is None and not (skip_receiver and arg.arg in ("self", "cls"))
    ]
    for extra in (node.args.vararg, node.args.kwarg):
        if extra is not None and extra.annotation is None:
            unannotated.append(extra.arg)
    returns = node.returns
    if returns is None:
        state = _RETURN_MISSING
    elif isinstance(returns, ast.Constant) and returns.value is None:
        state = _RETURN_NONE
    else:
        state = _RETURN_ANNOTATED
    return unannotated, state


def test_new_onepassword_test_classes_fully_annotate_public_methods() -> None:
    """Keep the three #201 test classes' public methods fully annotated.

    The repository's type-check command covers only ``src/``, so
    CONTRIBUTING.md's annotation rule is unenforced in tests. The check is
    scoped to the three classes the #201 work added; legacy test methods
    elsewhere keep their shape.
    """
    scope = (
        ("test_core_launcher.py", "TestProfilePreferences"),
        ("test_onepassword_lifecycle.py", "TestAutomaticRegistration"),
        ("test_onepassword_cli.py", "TestCliSurface"),
    )
    problems: list[str] = []
    for filename, class_name in scope:
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
            unannotated, return_state = _annotation_report(method, skip_receiver=True)
            parts = []
            if unannotated:
                parts.append("missing annotations for " + ", ".join(unannotated))
            if return_state != _RETURN_NONE:
                parts.append("missing -> None")
            if parts:
                problems.append(f"{filename}::{class_name}.{method.name}: " + "; ".join(parts))
    assert not problems, (
        "CONTRIBUTING.md requires type annotations on all public functions. The "
        "#201 test classes' public methods must annotate every parameter "
        "(pytest.MonkeyPatch, Path, and the fixtures' return types) and use "
        "-> None. Unannotated: " + "; ".join(problems)
    )


def test_conftest_source_isolation_fixture_is_fully_annotated() -> None:
    """Keep the suite's official-source isolation fixture fully annotated.

    The fixture is the suite-wide boundary between general unit tests and the
    developer's real 1Password installation, so CONTRIBUTING.md's annotation
    rule applies to it even though the repository's type-check command covers
    only ``src/``. Scoped to this one public conftest fixture; the legacy
    conftest functions keep their shape.
    """
    source = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="conftest.py")
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "isolated_official_onepassword_source"
    ]
    problems: list[str] = []
    if not matches:
        problems.append("tests/conftest.py: fixture isolated_official_onepassword_source not found")
    else:
        fixture = matches[0]
        unannotated, return_state = _annotation_report(fixture)
        parts = []
        if unannotated:
            parts.append("missing annotations for " + ", ".join(unannotated))
        if return_state == _RETURN_MISSING:
            parts.append("missing return annotation")
        elif return_state == _RETURN_NONE:
            parts.append(
                "return annotation must name the fixture's yield type "
                "(e.g. Iterator[None]), not None"
            )
        if parts:
            problems.append(
                "tests/conftest.py::isolated_official_onepassword_source: " + "; ".join(parts)
            )
    assert not problems, (
        "CONTRIBUTING.md requires type annotations on all public functions. "
        "The suite's official-source isolation fixture is a public boundary: "
        "annotate every parameter and the generator return. Unannotated: " + "; ".join(problems)
    )


def test_launcher_spawn_helpers_reject_guard_is_fully_annotated() -> None:
    """Keep the spawn-environment reject guard annotated in its own module.

    The guard moved from ``tests/conftest.py`` to the uniquely named
    ``tests/launcher_spawn_helpers.py`` because a flat ``conftest`` import
    is ambiguous in a whole-suite run (``tests/parity/conftest.py`` puts its
    directory on ``sys.path``). The same scoped annotation rule as the
    conftest isolation fixture applies, plus its boundary documentation:
    the guard's docstring must keep documenting what it refuses.
    """
    source = (Path(__file__).parent / "launcher_spawn_helpers.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="launcher_spawn_helpers.py")
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "discard_spawn_environment"
    ]
    problems: list[str] = []
    if not matches:
        problems.append("tests/launcher_spawn_helpers.py: discard_spawn_environment not found")
    else:
        guard = matches[0]
        unannotated, return_state = _annotation_report(guard)
        if unannotated:
            problems.append("missing annotations for " + ", ".join(unannotated))
        if return_state == _RETURN_MISSING:
            problems.append("missing return annotation")
        doc = ast.get_docstring(guard) or ""
        for section in ("Args:", "Raises:"):
            if section not in doc:
                problems.append(f"docstring missing {section}")
    assert not problems, (
        "CONTRIBUTING.md requires type annotations on all public functions. "
        "The suite-wide spawn reject guard is a public boundary shared by "
        "every spawn double. Unannotated or undocumented: " + "; ".join(problems)
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
        "a brand-new Named profile or a temporary unnamed launch is provisioned",
        "pins the official stable 1Password Chrome extension",
        "An existing profile is not registered automatically",
        "the pin seed never reads or rewrites an existing preference document, "
        "pin choice, or pinned list",
        "The password-manager preference updates that every launch already performs",
        "Without the flag, other platforms, the Camoufox engine, and machines "
        "without the official registration entry keep a plain launch",
        "A present but invalid official registration, including a link whose target is gone",
        "removes the empty new profile directory",
        "unchanged invalid registration is refused again",
        "a repaired one registers",
        "A cleanup that itself fails leaves the failed session directory in place",
        "it is never reused",
        "is kept unchanged",
        "provision the existing profile with --1password",
        "relaunching creates a fresh one",
    ):
        assert requirement.lower() in section.lower(), (
            f"bt guide's automatic-registration paragraph must state {requirement!r}. "
            "Keep the automatic default, its boundaries, the pin default, and its "
            "retry behavior documented."
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
