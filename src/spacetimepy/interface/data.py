"""Public, transport-neutral access to captured SpaceTime data.

The ORM model is an internal persistence detail.  This module exposes frozen
DTOs that can be consumed directly by Python integrations and serialized by a
future HTTP adapter without giving callers live SQLAlchemy objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from spacetimepy.core.model import (
    CodeDefinition,
    ExecutionBranch,
    ExecutionSession,
    ExecutionStep,
    ExternalInteractionOccurrence,
    FunctionCall,
    StackSnapshot,
    StoredObject,
)
from spacetimepy.core.serialization import PickleSerializer

if TYPE_CHECKING:
    import datetime
    from collections.abc import Collection

    from sqlalchemy.orm import Session


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
    ) -> None:
        self._database = database
        self._serializer = serializer or PickleSerializer()

    def list_sessions(self) -> tuple[SessionDTO, ...]:
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
        return self._call_to_dto(
            self._require(FunctionCall, call_id, "function call")
        )

    def get_stack_snapshot(self, snapshot_id: int) -> StackSnapshotDTO:
        return self._snapshot_to_dto(
            self._require(StackSnapshot, snapshot_id, "stack snapshot")
        )

    def get_code_definition(self, definition_id: str) -> CodeDefinitionDTO:
        definition = self._require(
            CodeDefinition, definition_id, "code definition"
        )
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
        return self._deserialize(
            self._require(StoredObject, reference, "stored value")
        )

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
        return {
            name: values[reference]
            for name, reference in references.items()
        }

    def resolve_branch_step_ids(self, branch_id: int) -> tuple[int, ...]:
        """Return IDs in the complete inherited-and-recomputed branch path."""

        branch = self._require(ExecutionBranch, branch_id, "execution branch")
        return tuple(self._id(step, "execution step") for step in self._resolve_step_models(branch))

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
                raise TraceConsistencyError(
                    f"Root branch {branch_id} has a fork step"
                )
            return own_steps

        if branch.forked_from_step is None:
            raise TraceConsistencyError(
                f"Child branch {branch_id} has no fork step"
            )

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
            key=lambda branch: (branch.created_at, branch.id or 0),
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
                for child in sorted(branch.child_branches, key=lambda child: child.id or 0)
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
        )

    def _snapshot_to_dto(self, snapshot: StackSnapshot) -> StackSnapshotDTO:
        return StackSnapshotDTO(
            id=self._id(snapshot, "stack snapshot"),
            function_call_id=snapshot.function_call_id,
            code_definition_id=(
                snapshot.code_definition_id
                or snapshot.function_call.code_definition_id
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
        value = self._database.get(model, identifier)
        if value is None:
            raise TraceNotFoundError(f"No {label} with id {identifier!r}")
        return value

    @staticmethod
    def _id(value: Any, label: str) -> int:
        if value.id is None:
            raise TraceConsistencyError(f"Unpersisted {label} has no id")
        return value.id


__all__ = [
    "BranchDTO",
    "BranchSummaryDTO",
    "CodeDefinitionDTO",
    "ExternalInteractionDTO",
    "FunctionCallDTO",
    "SessionDTO",
    "StackSnapshotDTO",
    "StepDTO",
    "StoredValueDTO",
    "TraceConsistencyError",
    "TraceData",
    "TraceDataError",
    "TraceNotFoundError",
]
