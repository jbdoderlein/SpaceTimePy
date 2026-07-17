"""Declarative types for standard-library external interactions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StandardExternalInteraction:
    """One callable resolved only after its owning module has loaded."""

    attribute_path: str
    ignored_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class StandardExternalModule:
    """External-interaction definitions belonging to one Python module."""

    module_name: str
    interactions: tuple[StandardExternalInteraction, ...]


__all__ = ["StandardExternalInteraction", "StandardExternalModule"]
