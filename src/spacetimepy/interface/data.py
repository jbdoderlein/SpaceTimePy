"""Public, transport-neutral access to captured SpaceTime data.

The ORM model is an internal persistence detail.  This module exposes frozen
DTOs that can be consumed directly by Python integrations and serialized by a
HTTP adapter without giving callers live SQLAlchemy objects.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, create_engine, func, inspect, select
from sqlalchemy.orm import Session

from spacetimepy.core.model import (
    Base,
    CodeDefinition,
    ExecutionBranch,
    ExecutionSession,
    ExecutionStep,
    ExternalInteractionOccurrence,
    FunctionCall,
    FunctionCallCapturePerformance,
    ObjectIdentity,
    StackSnapshot,
    StoredObject,
)
from spacetimepy.core.serialization import CustomPickler, PickleSerializer

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable


class TraceDataError(RuntimeError):
    """Base error raised by the programmatic trace-data interface."""


class TraceNotFoundError(TraceDataError):
    """Raised when a requested trace entity does not exist."""


class TraceConsistencyError(TraceDataError):
    """Raised when persisted branch data violates a model invariant."""


@dataclass(frozen=True, slots=True)
class StoredValueDTO:
    reference: str
    identity_id: int
    version: int
    type_name: str
    is_primitive: bool
    value: Any


@dataclass(frozen=True, slots=True)
class StoredValueSummaryDTO:
    reference: str
    identity_id: int
    version: int
    type_name: str
    is_primitive: bool


@dataclass(frozen=True, slots=True)
class TraceStatisticsDTO:
    session_count: int
    branch_count: int
    step_count: int
    function_call_count: int
    function_call_capture_performance_count: int
    stack_snapshot_count: int
    external_interaction_count: int
    object_identity_count: int
    stored_value_count: int
    code_definition_count: int


@dataclass(frozen=True, slots=True)
class CodeDefinitionDTO:
    id: str
    name: str
    qualified_name: str | None
    kind: str
    module_path: str
    code_content: str
    first_line_number: int | None
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class FunctionCallCapturePerformanceDTO:
    function_call_id: int
    start_capture_ns: int
    return_capture_ns: int
    unwind_capture_ns: int
    line_capture_ns: int
    direct_capture_ns: int
    inclusive_capture_ns: int
    line_event_count: int
    line_snapshot_count: int
    filtered_line_event_count: int
    line_capture_min_ns: int | None
    line_capture_max_ns: int | None

    @property
    def direct_capture_ms(self) -> float:
        return self.direct_capture_ns / 1_000_000

    @property
    def inclusive_capture_ms(self) -> float:
        return self.inclusive_capture_ns / 1_000_000

    @property
    def line_capture_average_ns(self) -> float | None:
        if self.line_event_count == 0:
            return None
        return self.line_capture_ns / self.line_event_count


@dataclass(frozen=True, slots=True)
class FunctionCallDTO:
    id: int
    function_name: str
    qualified_name: str | None
    module_name: str | None
    file_path: str | None
    first_line_number: int | None
    started_at: datetime.datetime
    completed_at: datetime.datetime | None
    outcome: str
    entry_local_references: dict[str, str]
    entry_global_references: dict[str, str]
    return_reference: str | None
    exception_reference: str | None
    attributes: dict[str, Any]
    code_definition_id: str | None
    caller_call_id: int | None
    capture_performance: FunctionCallCapturePerformanceDTO | None


@dataclass(frozen=True, slots=True)
class StackSnapshotDTO:
    id: int
    function_call_id: int
    code_definition_id: str | None
    line_number: int
    instruction_offset: int | None
    captured_at: datetime.datetime
    local_references: dict[str, str]
    global_references: dict[str, str]
    attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExternalInteractionDTO:
    id: int
    step_id: int
    position: int
    call: FunctionCallDTO


@dataclass(frozen=True, slots=True)
class StepDTO:
    id: int
    branch_id: int
    position: int
    kind: str
    source_step_id: int | None
    label: str | None
    annotations: dict[str, Any]
    created_at: datetime.datetime
    function_call: FunctionCallDTO | None
    stack_snapshot: StackSnapshotDTO | None
    external_interactions: tuple[ExternalInteractionDTO, ...]


@dataclass(frozen=True, slots=True)
class BranchSummaryDTO:
    id: int
    session_id: int
    parent_branch_id: int | None
    forked_from_step_id: int | None
    name: str | None
    status: str
    configuration_key: str | None
    created_at: datetime.datetime
    completed_at: datetime.datetime | None
    own_step_count: int


@dataclass(frozen=True, slots=True)
class BranchDTO:
    id: int
    session_id: int
    parent_branch_id: int | None
    forked_from_step_id: int | None
    child_branch_ids: tuple[int, ...]
    name: str | None
    status: str
    configuration_key: str | None
    recipe: dict[str, Any]
    attributes: dict[str, Any]
    created_at: datetime.datetime
    completed_at: datetime.datetime | None
    steps: tuple[StepDTO, ...]
    is_resolved_path: bool


@dataclass(frozen=True, slots=True)
class SessionDTO:
    id: int
    name: str | None
    description: str | None
    step_kind: str
    status: str
    created_at: datetime.datetime
    completed_at: datetime.datetime | None
    attributes: dict[str, Any]
    root_branch_id: int | None
    branches: tuple[BranchSummaryDTO, ...]


class TraceData:
    """Read captured data without exposing the internal ORM model."""

    def __init__(
        self,
        database: Session,
        serializer: PickleSerializer | None = None,
        *,
        engine: Engine | None = None,
        owns_database: bool = False,
        database_label: str | None = None,
    ) -> None:
        self._database = database
        self._serializer = serializer or PickleSerializer()
        self._engine = engine
        self._owns_database = owns_database
        self._database_label = database_label
        self._closed = False

    @classmethod
    def open(
        cls,
        database: str | Path,
        *,
        custom_picklers: Iterable[CustomPickler] = (),
        create_if_missing: bool = False,
    ) -> TraceData:
        """Open a trace database for read-oriented exploration.

        Unlike :meth:`SpaceTime.open`, this does not initialize monitoring or
        normally create a schema. ``create_if_missing`` may initialize an empty
        v2 SQLite trace at a filesystem path, which is useful when an explorer
        must start before the first recording. It never modifies an existing
        file without a v2 schema. Pickled values are executable data, so callers
        must only open trace files they trust.
        """

        database_label = str(database)
        create_schema = False
        if isinstance(database, Path):
            selected_path = database.expanduser().resolve()
            if selected_path.exists() and not selected_path.is_file():
                raise TraceDataError(
                    f"Trace database path is not a file: {selected_path}"
                )
            if not selected_path.exists() and not create_if_missing:
                raise FileNotFoundError(f"Trace database not found: {selected_path}")
            if not selected_path.exists():
                if not selected_path.parent.is_dir():
                    raise FileNotFoundError(
                        f"Trace database parent directory not found: "
                        f"{selected_path.parent}"
                    )
                create_schema = True
            url = f"sqlite+pysqlite:///{selected_path}"
            database_label = str(selected_path)
        elif "://" in database:
            if create_if_missing:
                raise ValueError(
                    "create_if_missing only supports a SQLite filesystem path"
                )
            url = database
        else:
            selected_path = Path(database).expanduser().resolve()
            if selected_path.exists() and not selected_path.is_file():
                raise TraceDataError(
                    f"Trace database path is not a file: {selected_path}"
                )
            if not selected_path.exists() and not create_if_missing:
                raise FileNotFoundError(f"Trace database not found: {selected_path}")
            if not selected_path.exists():
                if not selected_path.parent.is_dir():
                    raise FileNotFoundError(
                        f"Trace database parent directory not found: "
                        f"{selected_path.parent}"
                    )
                create_schema = True
            url = f"sqlite+pysqlite:///{selected_path}"
            database_label = str(selected_path)

        engine = create_engine(url)
        if create_schema:
            Base.metadata.create_all(engine)
        if not inspect(engine).has_table("execution_sessions"):
            engine.dispose()
            raise TraceDataError(
                "The database does not contain a SpaceTimePy v2 trace schema"
            )
        database_session = Session(engine, expire_on_commit=False)
        return cls(
            database_session,
            PickleSerializer(custom_picklers),
            engine=engine,
            owns_database=True,
            database_label=database_label,
        )

    @property
    def database_label(self) -> str | None:
        return self._database_label

    @property
    def is_closed(self) -> bool:
        return self._closed

    def refresh(self) -> None:
        """Expire cached rows so following reads observe committed changes."""

        self._ensure_open()
        if self._owns_database:
            # End a path-backed reader transaction before looking for commits
            # produced by a recording process that uses the same SQLite file.
            self._database.rollback()
        self._database.expire_all()

    def close(self) -> None:
        """Close resources owned by :meth:`open`; borrowed sessions stay open."""

        if self._closed:
            return
        if self._owns_database:
            self._database.close()
            if self._engine is not None:
                self._engine.dispose()
        self._closed = True

    def __enter__(self) -> TraceData:
        self._ensure_open()
        return self

    def __exit__(self, *exception: object) -> None:
        del exception
        self.close()

    def list_sessions(self) -> tuple[SessionDTO, ...]:
        self._ensure_open()
        sessions = self._database.scalars(
            select(ExecutionSession).order_by(ExecutionSession.created_at)
        ).all()
        return tuple(self._session_to_dto(session) for session in sessions)

    def get_session(self, session_id: int) -> SessionDTO:
        return self._session_to_dto(
            self._require(ExecutionSession, session_id, "execution session")
        )

    def get_branch(self, branch_id: int, *, resolve: bool = False) -> BranchDTO:
        branch = self._require(ExecutionBranch, branch_id, "execution branch")
        steps = self._resolve_step_models(branch) if resolve else list(branch.steps)
        return self._branch_to_dto(branch, steps, resolved=resolve)

    def get_step(self, step_id: int) -> StepDTO:
        return self._step_to_dto(
            self._require(ExecutionStep, step_id, "execution step")
        )

    def get_function_call(self, call_id: int) -> FunctionCallDTO:
        return self._call_to_dto(self._require(FunctionCall, call_id, "function call"))

    def get_function_call_performance(
        self,
        call_id: int,
    ) -> FunctionCallCapturePerformanceDTO | None:
        """Return capture-overhead metrics, or ``None`` when profiling was off."""

        call = self._require(FunctionCall, call_id, "function call")
        return (
            self._performance_to_dto(call.capture_performance)
            if call.capture_performance is not None
            else None
        )

    def get_stack_snapshot(self, snapshot_id: int) -> StackSnapshotDTO:
        return self._snapshot_to_dto(
            self._require(StackSnapshot, snapshot_id, "stack snapshot")
        )

    def get_code_definition(self, definition_id: str) -> CodeDefinitionDTO:
        definition = self._require(CodeDefinition, definition_id, "code definition")
        return CodeDefinitionDTO(
            id=definition.id,
            name=definition.name,
            qualified_name=definition.qualified_name,
            kind=definition.kind,
            module_path=definition.module_path,
            code_content=definition.code_content,
            first_line_number=definition.first_line_number,
            created_at=definition.created_at,
        )

    def list_function_calls(self) -> tuple[FunctionCallDTO, ...]:
        """Return all observed VM calls in chronological order."""

        self._ensure_open()
        calls = self._database.scalars(
            select(FunctionCall).order_by(FunctionCall.started_at, FunctionCall.id)
        ).all()
        return tuple(self._call_to_dto(call) for call in calls)

    def list_callee_calls(self, call_id: int) -> tuple[FunctionCallDTO, ...]:
        """Return direct VM children of a function call."""

        self.get_function_call(call_id)
        calls = self._database.scalars(
            select(FunctionCall)
            .where(FunctionCall.caller_call_id == call_id)
            .order_by(FunctionCall.started_at, FunctionCall.id)
        ).all()
        return tuple(self._call_to_dto(call) for call in calls)

    def list_function_call_performances(
        self,
    ) -> tuple[FunctionCallCapturePerformanceDTO, ...]:
        """Return every available per-call capture profile in call order."""

        self._ensure_open()
        performances = self._database.scalars(
            select(FunctionCallCapturePerformance).order_by(
                FunctionCallCapturePerformance.function_call_id
            )
        ).all()
        return tuple(self._performance_to_dto(item) for item in performances)

    def list_stack_snapshots(
        self,
        call_id: int | None = None,
    ) -> tuple[StackSnapshotDTO, ...]:
        """Return line snapshots, optionally restricted to one call."""

        self._ensure_open()
        statement = select(StackSnapshot)
        if call_id is not None:
            self.get_function_call(call_id)
            statement = statement.where(StackSnapshot.function_call_id == call_id)
        snapshots = self._database.scalars(
            statement.order_by(StackSnapshot.captured_at, StackSnapshot.id)
        ).all()
        return tuple(self._snapshot_to_dto(snapshot) for snapshot in snapshots)

    def list_code_definitions(self) -> tuple[CodeDefinitionDTO, ...]:
        """Return stored source definitions in creation order."""

        self._ensure_open()
        definitions = self._database.scalars(
            select(CodeDefinition).order_by(
                CodeDefinition.created_at,
                CodeDefinition.id,
            )
        ).all()
        return tuple(
            CodeDefinitionDTO(
                id=definition.id,
                name=definition.name,
                qualified_name=definition.qualified_name,
                kind=definition.kind,
                module_path=definition.module_path,
                code_content=definition.code_content,
                first_line_number=definition.first_line_number,
                created_at=definition.created_at,
            )
            for definition in definitions
        )

    def list_stored_values(self) -> tuple[StoredValueSummaryDTO, ...]:
        """List stored object versions without deserializing their values."""

        self._ensure_open()
        stored_values = self._database.scalars(
            select(StoredObject).order_by(
                StoredObject.identity_id,
                StoredObject.version_number,
            )
        ).all()
        return tuple(
            StoredValueSummaryDTO(
                reference=stored.id,
                identity_id=stored.identity_id,
                version=stored.version_number,
                type_name=stored.type_name,
                is_primitive=stored.is_primitive,
            )
            for stored in stored_values
        )

    def get_statistics(self) -> TraceStatisticsDTO:
        """Return inexpensive entity counts for service and explorer clients."""

        self._ensure_open()

        def count(model: type[Any]) -> int:
            return int(
                self._database.scalar(select(func.count()).select_from(model)) or 0
            )

        return TraceStatisticsDTO(
            session_count=count(ExecutionSession),
            branch_count=count(ExecutionBranch),
            step_count=count(ExecutionStep),
            function_call_count=count(FunctionCall),
            function_call_capture_performance_count=count(
                FunctionCallCapturePerformance
            ),
            stack_snapshot_count=count(StackSnapshot),
            external_interaction_count=count(ExternalInteractionOccurrence),
            object_identity_count=count(ObjectIdentity),
            stored_value_count=count(StoredObject),
            code_definition_count=count(CodeDefinition),
        )

    def get_stored_value(self, reference: str) -> StoredValueDTO:
        stored = self._require(StoredObject, reference, "stored value")
        return StoredValueDTO(
            reference=stored.id,
            identity_id=stored.identity_id,
            version=stored.version_number,
            type_name=stored.type_name,
            is_primitive=stored.is_primitive,
            value=self._deserialize(stored),
        )

    def load_value(self, reference: str | None) -> Any:
        """Materialize one value from a trusted trace, or ``None`` for null."""

        if reference is None:
            return None
        return self._deserialize(self._require(StoredObject, reference, "stored value"))

    def load_values(self, references: Collection[str]) -> dict[str, Any]:
        """Materialize several references with one database query.

        Non-primitive values use pickle and therefore trace databases must be
        treated as trusted input.
        """

        unique_references = tuple(dict.fromkeys(references))
        if not unique_references:
            return {}
        stored_values = self._database.scalars(
            select(StoredObject).where(StoredObject.id.in_(unique_references))
        ).all()
        by_reference = {stored.id: stored for stored in stored_values}
        missing = set(unique_references).difference(by_reference)
        if missing:
            missing_list = ", ".join(repr(reference) for reference in sorted(missing))
            raise TraceNotFoundError(f"No stored value(s) with id {missing_list}")
        return {
            reference: self._deserialize(by_reference[reference])
            for reference in unique_references
        }

    def load_references(self, references: dict[str, str]) -> dict[str, Any]:
        """Materialize a captured locals/globals reference mapping."""

        values = self.load_values(references.values())
        return {name: values[reference] for name, reference in references.items()}

    def resolve_branch_step_ids(self, branch_id: int) -> tuple[int, ...]:
        """Return IDs in the complete inherited-and-recomputed branch path."""

        branch = self._require(ExecutionBranch, branch_id, "execution branch")
        return tuple(
            self._id(step, "execution step")
            for step in self._resolve_step_models(branch)
        )

    def _resolve_step_models(
        self,
        branch: ExecutionBranch,
        ancestors: frozenset[int] = frozenset(),
    ) -> list[ExecutionStep]:
        branch_id = self._id(branch, "execution branch")
        if branch_id in ancestors:
            raise TraceConsistencyError(
                f"Cycle detected in branch ancestry at branch {branch_id}"
            )

        own_steps = list(branch.steps)
        if branch.parent_branch is None:
            if branch.forked_from_step is not None:
                raise TraceConsistencyError(f"Root branch {branch_id} has a fork step")
            return own_steps

        if branch.forked_from_step is None:
            raise TraceConsistencyError(f"Child branch {branch_id} has no fork step")

        parent_path = self._resolve_step_models(
            branch.parent_branch, ancestors | {branch_id}
        )
        fork_id = self._id(branch.forked_from_step, "execution step")
        for index, step in enumerate(parent_path):
            if self._id(step, "execution step") == fork_id:
                return parent_path[:index] + own_steps

        raise TraceConsistencyError(
            f"Fork step {fork_id} is not in parent path of branch {branch_id}"
        )

    def _session_to_dto(self, session: ExecutionSession) -> SessionDTO:
        branches = sorted(
            session.branches,
            key=lambda branch: (
                self._datetime_sort_key(branch.created_at),
                branch.id or 0,
            ),
        )
        roots = [branch for branch in branches if branch.parent_branch_id is None]
        root_id = self._id(roots[0], "execution branch") if len(roots) == 1 else None
        return SessionDTO(
            id=self._id(session, "execution session"),
            name=session.name,
            description=session.description,
            step_kind=session.step_kind.value,
            status=session.status.value,
            created_at=session.created_at,
            completed_at=session.completed_at,
            attributes=dict(session.attributes),
            root_branch_id=root_id,
            branches=tuple(self._branch_summary_to_dto(branch) for branch in branches),
        )

    @staticmethod
    def _datetime_sort_key(value: datetime.datetime) -> datetime.datetime:
        """Normalize SQLite-loaded and newly-created timestamps for sorting."""
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC).replace(tzinfo=None)
        return value

    def _branch_summary_to_dto(self, branch: ExecutionBranch) -> BranchSummaryDTO:
        return BranchSummaryDTO(
            id=self._id(branch, "execution branch"),
            session_id=branch.session_id,
            parent_branch_id=branch.parent_branch_id,
            forked_from_step_id=branch.forked_from_step_id,
            name=branch.name,
            status=branch.status.value,
            configuration_key=branch.configuration_key,
            created_at=branch.created_at,
            completed_at=branch.completed_at,
            own_step_count=len(branch.steps),
        )

    def _branch_to_dto(
        self,
        branch: ExecutionBranch,
        steps: list[ExecutionStep],
        *,
        resolved: bool,
    ) -> BranchDTO:
        return BranchDTO(
            id=self._id(branch, "execution branch"),
            session_id=branch.session_id,
            parent_branch_id=branch.parent_branch_id,
            forked_from_step_id=branch.forked_from_step_id,
            child_branch_ids=tuple(
                self._id(child, "execution branch")
                for child in sorted(
                    branch.child_branches, key=lambda child: child.id or 0
                )
            ),
            name=branch.name,
            status=branch.status.value,
            configuration_key=branch.configuration_key,
            recipe=dict(branch.recipe),
            attributes=dict(branch.attributes),
            created_at=branch.created_at,
            completed_at=branch.completed_at,
            steps=tuple(self._step_to_dto(step) for step in steps),
            is_resolved_path=resolved,
        )

    def _step_to_dto(self, step: ExecutionStep) -> StepDTO:
        return StepDTO(
            id=self._id(step, "execution step"),
            branch_id=step.branch_id,
            position=step.position,
            kind=step.kind.value,
            source_step_id=step.source_step_id,
            label=step.label,
            annotations=dict(step.annotations),
            created_at=step.created_at,
            function_call=(
                self._call_to_dto(step.function_call)
                if step.function_call is not None
                else None
            ),
            stack_snapshot=(
                self._snapshot_to_dto(step.stack_snapshot)
                if step.stack_snapshot is not None
                else None
            ),
            external_interactions=tuple(
                self._external_to_dto(occurrence)
                for occurrence in step.external_interactions
            ),
        )

    def _external_to_dto(
        self, occurrence: ExternalInteractionOccurrence
    ) -> ExternalInteractionDTO:
        return ExternalInteractionDTO(
            id=self._id(occurrence, "external interaction"),
            step_id=occurrence.step_id,
            position=occurrence.position,
            call=self._call_to_dto(occurrence.function_call),
        )

    def _call_to_dto(self, call: FunctionCall) -> FunctionCallDTO:
        return FunctionCallDTO(
            id=self._id(call, "function call"),
            function_name=call.function_name,
            qualified_name=call.qualified_name,
            module_name=call.module_name,
            file_path=call.file_path,
            first_line_number=call.first_line_number,
            started_at=call.started_at,
            completed_at=call.completed_at,
            outcome=call.outcome.value,
            entry_local_references=dict(call.entry_locals_refs),
            entry_global_references=dict(call.entry_globals_refs),
            return_reference=call.return_ref,
            exception_reference=call.exception_ref,
            attributes=dict(call.attributes),
            code_definition_id=call.code_definition_id,
            caller_call_id=call.caller_call_id,
            capture_performance=(
                self._performance_to_dto(call.capture_performance)
                if call.capture_performance is not None
                else None
            ),
        )

    @staticmethod
    def _performance_to_dto(
        performance: FunctionCallCapturePerformance,
    ) -> FunctionCallCapturePerformanceDTO:
        return FunctionCallCapturePerformanceDTO(
            function_call_id=performance.function_call_id,
            start_capture_ns=performance.start_capture_ns,
            return_capture_ns=performance.return_capture_ns,
            unwind_capture_ns=performance.unwind_capture_ns,
            line_capture_ns=performance.line_capture_ns,
            direct_capture_ns=performance.direct_capture_ns,
            inclusive_capture_ns=performance.inclusive_capture_ns,
            line_event_count=performance.line_event_count,
            line_snapshot_count=performance.line_snapshot_count,
            filtered_line_event_count=performance.filtered_line_event_count,
            line_capture_min_ns=performance.line_capture_min_ns,
            line_capture_max_ns=performance.line_capture_max_ns,
        )

    def _snapshot_to_dto(self, snapshot: StackSnapshot) -> StackSnapshotDTO:
        return StackSnapshotDTO(
            id=self._id(snapshot, "stack snapshot"),
            function_call_id=snapshot.function_call_id,
            code_definition_id=(
                snapshot.code_definition_id or snapshot.function_call.code_definition_id
            ),
            line_number=snapshot.line_number,
            instruction_offset=snapshot.instruction_offset,
            captured_at=snapshot.captured_at,
            local_references=dict(snapshot.locals_refs),
            global_references=dict(snapshot.globals_refs),
            attributes=dict(snapshot.attributes),
        )

    def _deserialize(self, stored: StoredObject) -> Any:
        if stored.is_primitive:
            return stored.primitive_value
        if stored.pickle_data is None:
            raise TraceConsistencyError(
                f"Non-primitive stored value {stored.id!r} has no pickle data"
            )
        return self._serializer.loads(stored.pickle_data)

    def _require(self, model: type[Any], identifier: Any, label: str) -> Any:
        self._ensure_open()
        value = self._database.get(model, identifier)
        if value is None:
            raise TraceNotFoundError(f"No {label} with id {identifier!r}")
        return value

    @staticmethod
    def _id(value: Any, label: str) -> int:
        if value.id is None:
            raise TraceConsistencyError(f"Unpersisted {label} has no id")
        return value.id

    def _ensure_open(self) -> None:
        if self._closed:
            raise TraceDataError("This trace-data reader is closed")


__all__ = [
    "BranchDTO",
    "BranchSummaryDTO",
    "CodeDefinitionDTO",
    "ExternalInteractionDTO",
    "FunctionCallCapturePerformanceDTO",
    "FunctionCallDTO",
    "SessionDTO",
    "StackSnapshotDTO",
    "StepDTO",
    "StoredValueDTO",
    "StoredValueSummaryDTO",
    "TraceConsistencyError",
    "TraceData",
    "TraceDataError",
    "TraceNotFoundError",
    "TraceStatisticsDTO",
]
