"""The one exception that means CLI exit code 2.

Three layers can reject an invocation before anything happens: ``passthrough``
(a malformed raw-protocol line), ``endpoint`` (a refused ``--endpoint``), and
``lifecycle`` (a profile name that is not one). They are separate modules with a
one-way import order, so none of them can own the type the other two raise, and
the CLI front was accumulating a tuple of unrelated classes that all mean the
same exit code.

This module sits below all three. Each keeps its own named subclass, so a caller
can still catch exactly the failure it handles, and the CLI catches the base.
"""

from __future__ import annotations


class UsageError(Exception):
    """A malformed or refused CLI invocation (exit code 2).

    Raised before the action happens: nothing is sent, written, or deleted.
    Operational failures -- the browser is gone, a CDP call failed, a deadline
    passed -- are ``lifecycle.LifecycleError`` (exit code 1) instead.
    """
