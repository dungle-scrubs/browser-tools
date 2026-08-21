"""Make the parity harness modules importable by flat name.

The harness modules (``parity_comparison``, ``parity_corpus``,
``parity_engines``) sit beside the parity tests and are imported by flat name
both here and from ``run_baseline.py`` (run as a script). Inserting this
directory on ``sys.path`` keeps a single import style working in both entry
contexts without making ``tests`` a package.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
