"""How a verb's free-form arguments name an instance and a value.

Two kinds of verb read their arguments out of a free-form positional list
instead of named arguments: ``help`` and ``attach`` name an instance, and
the RFC-05 verbs (``navigate``, ``eval``, ``press``, ``type``,
``wait-text``) name an instance and one value in the same list. Reading
those lists -- and telling an instance name apart from a value -- is what
this module owns, so ``cli.py`` keeps the dispatch and the exit codes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import lifecycle
from .passthrough import UsageError as PassthroughUsageError
from .usage import UsageError

if TYPE_CHECKING:
    import argparse


def one_instance(leading: str | None, inline: str | None) -> str | None:
    """Reconcile a leading ``INSTANCE`` with one inside the verb's own args.

    ``help`` and ``attach`` read their instance out of a free-form positional
    list rather than a named argument, so the conflict ``main`` catches for
    every other verb has to be caught here instead.

    Raises:
        UsageError: Both spellings were used.
    """
    if leading is not None and inline is not None:
        raise PassthroughUsageError(
            f"instance named twice: '{leading}' before the verb and '{inline}' after it"
        )
    return leading if leading is not None else inline


def rfc05_operands(args: argparse.Namespace, known_instances: set[str] | None) -> None:
    """Split one RFC-05 verb's free-form operands into an instance and a value.

    The verb's own field (``destination``, ``source``, ``key``, ``text``,
    ``substring``) takes the value, and the instance -- named before the verb
    or inside the operands -- is reconciled into one name.

    Raises:
        UsageError: The verb took more operands than it has fields for.
    """
    if not hasattr(args, "operands"):
        return
    operands: list[str] = args.operands
    if len(operands) > 2:
        raise UsageError(f"{args.command} takes one operand and an optional instance")
    inline = None
    value = None
    if len(operands) == 2:
        inline, value = operands
    elif operands:
        value = operands[0]
        if args.command == "type" and args.file is not None:
            known = known_instances
            if known is None:
                known = {
                    item.name
                    for item in lifecycle.read_instances(
                        registry_path=lifecycle.registry_path_from_env()
                    )
                }
            if value in known:
                inline, value = value, None
    args.instance = one_instance(args.instance, inline)
    field = {
        "navigate": "destination",
        "eval": "source",
        "press": "key",
        "type": "text",
        "wait-text": "substring",
    }[args.command]
    setattr(args, field, value)
    delattr(args, "operands")


__all__ = ["one_instance", "rfc05_operands"]
