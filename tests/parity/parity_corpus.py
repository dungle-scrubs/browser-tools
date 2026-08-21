"""The frozen parity corpus (RFC-01, Testing Strategy, "Parity gate").

The corpus is a fixed set of local static HTML fixtures under ``fixtures/``.
It is frozen before Phase 2 implementation starts and must not be edited to
make a gate pass: changing the corpus invalidates any baseline captured
against it. Local files keep the gate reproducible offline; no corpus page
depends on a live public site.

Each entry names the structural case it covers. The RFC requires the corpus to
include iframe and shadow DOM cases; both are present here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class CorpusPage:
    """One frozen corpus page."""

    page_id: str
    filename: str
    structural_case: str

    def path(self) -> Path:
        """Absolute path to the fixture file."""
        return FIXTURES_DIR / self.filename

    def file_url(self) -> str:
        """``file://`` URL an engine can navigate to."""
        return self.path().as_uri()


# The frozen corpus. Order is informational only; the operator is
# order-insensitive across pages and within a page's node set.
CORPUS: tuple[CorpusPage, ...] = (
    CorpusPage("plain", "plain.html", "plain static page: headings, prose, links"),
    CorpusPage("form", "form.html", "form: inputs, checkbox, and buttons for UID interaction"),
    CorpusPage("iframe", "iframe.html", "iframe: accessibility tree crosses a frame boundary"),
    CorpusPage("shadow", "shadow.html", "shadow DOM: controls inside an open shadow root"),
    CorpusPage("dynamic", "dynamic.html", "dynamic content: nodes added after initial paint"),
)

# A support file referenced by iframe.html; not a corpus entry on its own.
SUPPORT_FILES: tuple[str, ...] = ("iframe_child.html",)


def corpus_page(page_id: str) -> CorpusPage:
    """Look up a corpus page by id."""
    for page in CORPUS:
        if page.page_id == page_id:
            return page
    raise KeyError(f"no corpus page with id {page_id!r}")
