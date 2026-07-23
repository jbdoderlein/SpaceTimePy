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

from .alignment import (
    AlignmentAlgorithmDescriptor,
    AlignmentAlgorithmNotFoundError,
    AlignmentData,
    AlignmentError,
    AlignmentLink,
    AlignmentRegistry,
    AlignmentRelation,
    AlignmentResult,
    AlignmentService,
    AlignmentValidationError,
    CodeDiffProvider,
    CodeDiffProviderNotFoundError,
    OfflineAlignmentAlgorithm,
    OfflineAlignmentContext,
    OnlineAlignmentAlgorithm,
    OnlineAlignmentContext,
    OnlineAlignmentRun,
    OnlineAlignmentSession,
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
from .mcp.service import AgentTraceService
from .replay import (
    ExternalInteractionScript,
    RecordedExternalInteraction,
    ReplayAlignmentPolicy,
    ReplayContext,
    ReplayDivergenceError,
    ReplayError,
    ReplayInterface,
    ReplayResult,
)
from .runtime import SpaceTime, get_active_spacetime, open_spacetime
from .stack_snapshot_alignment import (
    STACK_SNAPSHOT_ALIGNMENT,
    STACK_SNAPSHOT_ALIGNMENT_VERSION,
    CodeDiffLineMapper,
    CodeDiffLineMappingError,
    CodeLineCorrespondence,
    CodeLineMapping,
    StackSnapshotAlignment,
)


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


def create_mcp_server(*args, **kwargs):
    """Lazily create the read-only SpaceTime trace MCP server."""

    from .mcp import create_mcp_server as create

    return create(*args, **kwargs)


def run_mcp(*args, **kwargs):
    """Lazily run the read-only SpaceTime trace MCP server."""

    from .mcp import run_mcp as run

    return run(*args, **kwargs)


__all__ = [
    "AgentTraceService",
    "AlignmentAlgorithmDescriptor",
    "AlignmentAlgorithmNotFoundError",
    "AlignmentData",
    "AlignmentError",
    "AlignmentLink",
    "AlignmentRegistry",
    "AlignmentRelation",
    "AlignmentResult",
    "AlignmentService",
    "AlignmentValidationError",
    "BranchDTO",
    "BranchSummaryDTO",
    "CaptureDeclaration",
    "CaptureInterface",
    "CaptureMode",
    "CaptureRegistry",
    "CaptureReturnContext",
    "CaptureStartContext",
    "CodeDefinitionDTO",
    "CodeDiffLineMapper",
    "CodeDiffLineMappingError",
    "CodeDiffProvider",
    "CodeDiffProviderNotFoundError",
    "CodeLineCorrespondence",
    "CodeLineMapping",
    "CustomPickler",
    "CustomPicklerError",
    "CustomPicklerProvider",
    "DispatchTable",
    "ExternalInteractionDTO",
    "ExternalInteractionScript",
    "FunctionCallDTO",
    "OfflineAlignmentAlgorithm",
    "OfflineAlignmentContext",
    "OnlineAlignmentAlgorithm",
    "OnlineAlignmentContext",
    "OnlineAlignmentRun",
    "OnlineAlignmentSession",
    "RecordedExternalInteraction",
    "RecordingHandle",
    "ReturnHook",
    "ReplayContext",
    "ReplayDivergenceError",
    "ReplayError",
    "ReplayAlignmentPolicy",
    "ReplayInterface",
    "ReplayResult",
    "Reducer",
    "SessionDTO",
    "SerializationError",
    "SpaceTime",
    "StackSnapshotDTO",
    "StackSnapshotAlignment",
    "StepDTO",
    "StoredValueDTO",
    "StoredValueSummaryDTO",
    "StartHook",
    "TraceConsistencyError",
    "TraceData",
    "TraceDataError",
    "TraceNotFoundError",
    "TraceStatisticsDTO",
    "STACK_SNAPSHOT_ALIGNMENT",
    "STACK_SNAPSHOT_ALIGNMENT_VERSION",
    "clear_capture_declarations",
    "create_api_app",
    "create_explorer_app",
    "create_mcp_server",
    "external",
    "function",
    "get_active_spacetime",
    "line",
    "open_spacetime",
    "run_api",
    "run_explorer",
    "run_mcp",
    "start_api",
    "support",
    "unregister_capture_declaration",
]
