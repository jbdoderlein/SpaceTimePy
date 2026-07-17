"""External-interaction definitions for Python's :mod:`random` module.

This descriptor intentionally does not import ``random``.  Its callable names
are resolved from the real module object after application code loads it.
"""

from __future__ import annotations

from .definitions import (
    StandardExternalInteraction,
    StandardExternalModule,
)

DEFINITION = StandardExternalModule(
    module_name="random",
    interactions=(
        StandardExternalInteraction(
            attribute_path="randint",
            # ``self`` is the module-level Random instance. Its large mutable
            # state is irrelevant when replaying the recorded return value.
            ignored_names=frozenset({"self"}),
        ),
    ),
)


__all__ = ["DEFINITION"]
