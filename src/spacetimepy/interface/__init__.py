"""Public SpaceTimePy v2 programmatic interface.

The web API is implemented as an adapter over these services and DTOs, not as
a second route into the internal ORM model.
"""

from spacetimepy.core.serialization import (
    CustomPickler,
    CustomPicklerError,
    CustomPicklerProvider,
    DispatchTable,
    Reducer,
    SerializationError,
)

from .capture import (
    CaptureDeclaration,
    CaptureInterface,
    CaptureMode,
    CaptureRegistry,
    CaptureReturnContext,
    CaptureStartContext,
    RecordingHandle,
    ReturnHook,
    StartHook,
    clear_capture_declarations,
    external,
    function,
    line,
    support,
    unregister_capture_declaration,
)
from .data import (
    BranchDTO,
    BranchSummaryDTO,
    CodeDefinitionDTO,
    ExternalInteractionDTO,
    FunctionCallDTO,
    SessionDTO,
    StackSnapshotDTO,
    StepDTO,
    StoredValueDTO,
    StoredValueSummaryDTO,
    TraceConsistencyError,
    TraceData,
    TraceDataError,
    TraceNotFoundError,
    TraceStatisticsDTO,
)
from .replay import (
    ExternalInteractionScript,
    RecordedExternalInteraction,
    ReplayContext,
    ReplayDivergenceError,
    ReplayError,
    ReplayInterface,
    ReplayResult,
)
from .runtime import SpaceTime, get_active_spacetime, open_spacetime


def create_api_app(*args, **kwargs):
    """Lazily create the SpaceTime JSON API application."""

    from .web import create_api_app as create

    return create(*args, **kwargs)


def create_explorer_app(*args, **kwargs):
    """Lazily create the combined JSON API and browser explorer."""

    from .web import create_explorer_app as create

    return create(*args, **kwargs)


def start_api(*args, **kwargs):
    """Lazily start the JSON API in a background thread."""

    from .web import start_api as start

    return start(*args, **kwargs)


def run_api(*args, **kwargs):
    """Lazily run the JSON API until interrupted."""

    from .web import run_api as run

    return run(*args, **kwargs)


def run_explorer(*args, **kwargs):
    """Lazily run the combined API and browser explorer."""

    from .web import run_explorer as run

    return run(*args, **kwargs)

__all__ = [
    "BranchDTO",
    "BranchSummaryDTO",
    "CaptureDeclaration",
    "CaptureInterface",
    "CaptureMode",
    "CaptureRegistry",
    "CaptureReturnContext",
    "CaptureStartContext",
    "CodeDefinitionDTO",
    "CustomPickler",
    "CustomPicklerError",
    "CustomPicklerProvider",
    "DispatchTable",
    "ExternalInteractionDTO",
    "ExternalInteractionScript",
    "FunctionCallDTO",
    "RecordedExternalInteraction",
    "RecordingHandle",
    "ReturnHook",
    "ReplayContext",
    "ReplayDivergenceError",
    "ReplayError",
    "ReplayInterface",
    "ReplayResult",
    "Reducer",
    "SessionDTO",
    "SerializationError",
    "SpaceTime",
    "StackSnapshotDTO",
    "StepDTO",
    "StoredValueDTO",
    "StoredValueSummaryDTO",
    "StartHook",
    "TraceConsistencyError",
    "TraceData",
    "TraceDataError",
    "TraceNotFoundError",
    "TraceStatisticsDTO",
    "clear_capture_declarations",
    "create_api_app",
    "create_explorer_app",
    "external",
    "function",
    "get_active_spacetime",
    "line",
    "open_spacetime",
    "run_api",
    "run_explorer",
    "start_api",
    "support",
    "unregister_capture_declaration",
]
