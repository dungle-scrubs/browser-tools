"""The in-band manual covers the parser's actual commands."""

import re

from browser_tools import cli, lifecycle


def test_guide_documents_every_registered_verb():
    documented = set(re.findall(r"^  ([a-z-]+)(?: |$)", lifecycle.guide_text(), re.MULTILINE))
    assert documented >= cli._KNOWN_VERBS
