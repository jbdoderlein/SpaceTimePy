"""Lazy standard-library external-interaction catalogue.

Each supported Python module has a descriptor file in this package. Descriptor
files contain names only and therefore never import the standard module they
describe.
"""

from .definitions import StandardExternalInteraction, StandardExternalModule
from .registry import (
    STANDARD_EXTERNAL_MODULES,
    StandardExternalInteractionRegistry,
)

__all__ = [
    "STANDARD_EXTERNAL_MODULES",
    "StandardExternalInteraction",
    "StandardExternalInteractionRegistry",
    "StandardExternalModule",
]
