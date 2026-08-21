"""Draft persistence model for the next breaking SpaceTimePy version.

The model is deliberately split into three layers:

1. Stored Python values and code definitions.
2. Neutral observations of the Python VM (calls and stack snapshots).
3. The organization of those observations into sessions, branches and steps.

The important rule is that VM observations do not know where they are displayed
in an exploration.  A :class:`FunctionCall` can describe its actual caller and
a :class:`StackSnapshot` belongs to its actual call, because those are Python
runtime facts.  Neither model contains a session, branch, sequence number,
replay edge, or fork point.  Those concerns live exclusively in
:class:`ExecutionSession`, :class:`ExecutionBranch` and
:class:`ExecutionStep`.

This is a proposal, not a compatibility layer.  It intentionally does not
provide aliases, migrations, serialization DTOs, or the database helpers from
``models.py``.

Branch invariants that need service-level validation
----------------------------------------------------

SQL constraints cover local row consistency.  The interface/service layer is
responsible for the cross-row invariants below:

* an execution session has exactly one root branch;
* all steps in a session use the session's ``step_kind``;
* a child branch's ``forked_from_step`` is in its parent's resolved path;
* positions are contiguous and start at zero within each branch suffix;
* a child branch has at least one step, whose ``source_step`` is its
  ``forked_from_step``;
* a step's ``source_step`` is in an ancestor path and has the same kind;
* external interaction positions are contiguous and start at zero within each
  step;
* an external interaction's function call occurred within its owning step's
  execution interval;
* the branch parent relation is acyclic.

A branch stores only its newly executed suffix.  Its complete path is the
inherited prefix strictly before ``forked_from_step`` followed by its own
steps.  The old step is the migration input; the first child step is its real,
post-migration replacement.  This avoids copying already-computed
calls/snapshots into every variant.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


def utc_now() -> datetime.datetime:
    """Return an aware timestamp suitable for client-side column defaults."""

    return datetime.datetime.now(datetime.UTC)


class Base(DeclarativeBase):
    pass


class StepKind(StrEnum):
    """The VM observation used as the timeline unit for a session."""

    FUNCTION_CALL = "function_call"
    STACK_SNAPSHOT = "stack_snapshot"


class ExecutionStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FunctionCallOutcome(StrEnum):
    RUNNING = "running"
    RETURNED = "returned"
    RAISED = "raised"


EnumType = TypeVar("EnumType", bound=StrEnum)


def enum_type(enum_class: type[EnumType], name: str) -> SqlEnum[EnumType]:
    """Create a portable enum that stores the public string values."""

    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


# ---------------------------------------------------------------------------
# Stored Python values and code
# ---------------------------------------------------------------------------


class ObjectIdentity(Base):
    """Stable identity of one live Python object across captured versions."""

    __tablename__ = "object_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    versions: Mapped[list[StoredObject]] = relationship(
        back_populates="identity",
        cascade="all, delete-orphan",
        order_by="StoredObject.version_number",
    )


class StoredObject(Base):
    """One serialized state/version of a Python object."""

    __tablename__ = "stored_objects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    identity_id: Mapped[int] = mapped_column(
        ForeignKey("object_identities.id"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    type_name: Mapped[str] = mapped_column(String, nullable=False)
    is_primitive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    primitive_value: Mapped[Any | None] = mapped_column(JSON)
    dill_data: Mapped[bytes | None] = mapped_column(LargeBinary)

    identity: Mapped[ObjectIdentity] = relationship(back_populates="versions")
    code_definitions: Mapped[list[CodeDefinition]] = relationship(
        secondary="code_object_links", back_populates="objects"
    )

    __table_args__ = (
        UniqueConstraint(
            "identity_id", "version_number", name="uq_stored_object_identity_version"
        ),
        CheckConstraint(
            "(is_primitive IS TRUE AND dill_data IS NULL) OR "
            "(is_primitive IS FALSE AND dill_data IS NOT NULL)",
            name="ck_stored_object_representation",
        ),
    )


class CodeDefinition(Base):
    """Content-addressed source definition used by captured VM records."""

    __tablename__ = "code_definitions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    qualified_name: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    module_path: Mapped[str] = mapped_column(String, nullable=False)
    code_content: Mapped[str] = mapped_column(Text, nullable=False)
    first_line_number: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    function_calls: Mapped[list[FunctionCall]] = relationship(
        back_populates="code_definition"
    )
    stack_snapshots: Mapped[list[StackSnapshot]] = relationship(
        back_populates="code_definition"
    )
    objects: Mapped[list[StoredObject]] = relationship(
        secondary="code_object_links", back_populates="code_definitions"
    )


class CodeObjectLink(Base):
    """Association between a stored object version and relevant source code."""

    __tablename__ = "code_object_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_id: Mapped[str] = mapped_column(
        ForeignKey("stored_objects.id"), nullable=False
    )
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("code_definitions.id"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "object_id", "definition_id", name="uq_code_object_definition"
        ),
        Index("idx_code_object_link_object", "object_id"),
        Index("idx_code_object_link_definition", "definition_id"),
    )


# ---------------------------------------------------------------------------
# Neutral Python VM observations
# ---------------------------------------------------------------------------


class FunctionCall(Base):
    """One observed Python function invocation.

    ``caller_call_id`` is deliberately retained: it means the actual caller on
    the Python stack, never a replay parent or branch parent.  Sibling ordering
    is represented by ``ExecutionStep.position`` instead of this model.
    """

    __tablename__ = "function_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    function_name: Mapped[str] = mapped_column(String, nullable=False)
    qualified_name: Mapped[str | None] = mapped_column(String)
    module_name: Mapped[str | None] = mapped_column(String)
    file_path: Mapped[str | None] = mapped_column(String)
    first_line_number: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    outcome: Mapped[FunctionCallOutcome] = mapped_column(
        enum_type(FunctionCallOutcome, "function_call_outcome"),
        default=FunctionCallOutcome.RUNNING,
        nullable=False,
    )

    entry_locals_refs: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    entry_globals_refs: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    return_ref: Mapped[str | None] = mapped_column(String)
    exception_ref: Mapped[str | None] = mapped_column(String)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    code_definition_id: Mapped[str | None] = mapped_column(
        ForeignKey("code_definitions.id"), index=True
    )
    caller_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("function_calls.id"), index=True
    )

    code_definition: Mapped[CodeDefinition | None] = relationship(
        back_populates="function_calls"
    )
    caller_call: Mapped[FunctionCall | None] = relationship(
        back_populates="callee_calls",
        foreign_keys=[caller_call_id],
        remote_side=[id],
    )
    callee_calls: Mapped[list[FunctionCall]] = relationship(
        back_populates="caller_call", foreign_keys=[caller_call_id]
    )
    stack_snapshots: Mapped[list[StackSnapshot]] = relationship(
        back_populates="function_call", cascade="all, delete-orphan"
    )
    capture_performance: Mapped[FunctionCallCapturePerformance | None] = relationship(
        back_populates="function_call",
        cascade="all, delete-orphan",
        lazy="joined",
        uselist=False,
    )

    __table_args__ = (
        CheckConstraint(
            "(outcome = 'running' AND completed_at IS NULL "
            "AND return_ref IS NULL AND exception_ref IS NULL) OR "
            "(outcome = 'returned' AND completed_at IS NOT NULL "
            "AND exception_ref IS NULL) OR "
            "(outcome = 'raised' AND completed_at IS NOT NULL "
            "AND return_ref IS NULL)",
            name="ck_function_call_outcome",
        ),
        CheckConstraint(
            "caller_call_id IS NULL OR caller_call_id <> id",
            name="ck_function_call_not_own_caller",
        ),
    )


class FunctionCallCapturePerformance(Base):
    """Optional capture-overhead measurements for one observed call.

    Durations use a monotonic clock and are stored as integer nanoseconds.
    They measure recorder work only: profiler bookkeeping and persistence of
    this row happen after each timed region and are deliberately excluded.
    Line fields stay zero/null when line capture is not active for the call.
    """

    __tablename__ = "function_call_capture_performance"

    function_call_id: Mapped[int] = mapped_column(
        ForeignKey("function_calls.id"), primary_key=True
    )
    start_capture_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    return_capture_ns: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    unwind_capture_ns: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    line_capture_ns: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    direct_capture_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inclusive_capture_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)

    line_event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    line_snapshot_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    filtered_line_event_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    line_capture_min_ns: Mapped[int | None] = mapped_column(BigInteger)
    line_capture_max_ns: Mapped[int | None] = mapped_column(BigInteger)

    function_call: Mapped[FunctionCall] = relationship(
        back_populates="capture_performance"
    )

    __table_args__ = (
        CheckConstraint(
            "start_capture_ns >= 0 AND return_capture_ns >= 0 "
            "AND unwind_capture_ns >= 0 AND line_capture_ns >= 0 "
            "AND direct_capture_ns >= 0 "
            "AND inclusive_capture_ns >= direct_capture_ns",
            name="ck_function_call_capture_performance_durations",
        ),
        CheckConstraint(
            "direct_capture_ns = start_capture_ns + return_capture_ns "
            "+ unwind_capture_ns + line_capture_ns",
            name="ck_function_call_capture_performance_direct_total",
        ),
        CheckConstraint(
            "line_event_count >= 0 AND line_snapshot_count >= 0 "
            "AND filtered_line_event_count >= 0 "
            "AND line_snapshot_count + filtered_line_event_count "
            "= line_event_count",
            name="ck_function_call_capture_performance_line_counts",
        ),
        CheckConstraint(
            "(line_event_count = 0 AND line_capture_min_ns IS NULL "
            "AND line_capture_max_ns IS NULL) OR "
            "(line_event_count > 0 AND line_capture_min_ns >= 0 "
            "AND line_capture_max_ns >= line_capture_min_ns)",
            name="ck_function_call_capture_performance_line_range",
        ),
    )


class StackSnapshot(Base):
    """Observed state of a call frame at one Python execution location.

    The owning call and effective code version are VM facts.  Chronological
    position, previous/next transitions and replay edges are not stored here.
    """

    __tablename__ = "stack_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    function_call_id: Mapped[int] = mapped_column(
        ForeignKey("function_calls.id"), nullable=False, index=True
    )
    code_definition_id: Mapped[str | None] = mapped_column(
        ForeignKey("code_definitions.id"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    instruction_offset: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    locals_refs: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    globals_refs: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    function_call: Mapped[FunctionCall] = relationship(
        back_populates="stack_snapshots"
    )
    code_definition: Mapped[CodeDefinition | None] = relationship(
        back_populates="stack_snapshots"
    )

    @property
    def effective_code_definition(self) -> CodeDefinition | None:
        """Code active at this point, falling back to code at call entry."""

        return self.code_definition or self.function_call.code_definition


# ---------------------------------------------------------------------------
# Exploration organization
# ---------------------------------------------------------------------------


class ExecutionSession(Base):
    """One user-visible exploration containing a tree of execution branches."""

    __tablename__ = "execution_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    step_kind: Mapped[StepKind] = mapped_column(
        enum_type(StepKind, "execution_step_kind"), nullable=False
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        enum_type(ExecutionStatus, "execution_session_status"),
        default=ExecutionStatus.OPEN,
        nullable=False,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    branches: Mapped[list[ExecutionBranch]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ExecutionBranch(Base):
    """A newly executed suffix forked from an existing session path.

    ``forked_from_step`` is both the user-selected branch point and the exact
    state from which SpaceTimePy restarts.  The first real step in this branch
    materializes the migrated data and new code, and uses ``forked_from_step``
    as its ``source_step``.
    """

    __tablename__ = "execution_branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("execution_sessions.id"), nullable=False, index=True
    )
    parent_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_branches.id"), index=True
    )
    forked_from_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_steps.id")
    )

    name: Mapped[str | None] = mapped_column(String)
    status: Mapped[ExecutionStatus] = mapped_column(
        enum_type(ExecutionStatus, "execution_branch_status"),
        default=ExecutionStatus.OPEN,
        nullable=False,
    )
    configuration_key: Mapped[str | None] = mapped_column(String, index=True)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    session: Mapped[ExecutionSession] = relationship(back_populates="branches")
    parent_branch: Mapped[ExecutionBranch | None] = relationship(
        back_populates="child_branches",
        foreign_keys=[parent_branch_id],
        remote_side=[id],
    )
    child_branches: Mapped[list[ExecutionBranch]] = relationship(
        back_populates="parent_branch", foreign_keys=[parent_branch_id]
    )
    steps: Mapped[list[ExecutionStep]] = relationship(
        back_populates="branch",
        cascade="all, delete-orphan",
        foreign_keys="ExecutionStep.branch_id",
        order_by="ExecutionStep.position",
    )
    forked_from_step: Mapped[ExecutionStep | None] = relationship(
        foreign_keys=[forked_from_step_id]
    )

    __table_args__ = (
        CheckConstraint(
            "(parent_branch_id IS NULL AND forked_from_step_id IS NULL) OR "
            "(parent_branch_id IS NOT NULL "
            "AND forked_from_step_id IS NOT NULL)",
            name="ck_execution_branch_root_or_fork",
        ),
        Index(
            "idx_execution_branch_configuration",
            "session_id",
            "configuration_key",
        ),
    )


class ExecutionStep(Base):
    """Placement of exactly one neutral VM observation in a branch suffix.

    ``source_step`` maps a recomputed result to the prior result it replaces or
    derives from.  The first step of a child branch uses the branch's
    ``forked_from_step`` as its source.  A genuinely new later step has no
    source.
    """

    __tablename__ = "execution_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("execution_branches.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[StepKind] = mapped_column(
        enum_type(StepKind, "execution_step_payload_kind"), nullable=False
    )

    function_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("function_calls.id"), unique=True
    )
    stack_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("stack_snapshots.id"), unique=True
    )
    source_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_steps.id"), index=True
    )

    label: Mapped[str | None] = mapped_column(String)
    annotations: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    branch: Mapped[ExecutionBranch] = relationship(
        back_populates="steps", foreign_keys=[branch_id]
    )
    function_call: Mapped[FunctionCall | None] = relationship()
    stack_snapshot: Mapped[StackSnapshot | None] = relationship()
    source_step: Mapped[ExecutionStep | None] = relationship(
        back_populates="derived_steps",
        foreign_keys=[source_step_id],
        remote_side=[id],
    )
    derived_steps: Mapped[list[ExecutionStep]] = relationship(
        back_populates="source_step", foreign_keys=[source_step_id]
    )
    external_interactions: Mapped[list[ExternalInteractionOccurrence]] = (
        relationship(
            back_populates="step",
            cascade="all, delete-orphan",
            order_by="ExternalInteractionOccurrence.position",
        )
    )

    __table_args__ = (
        UniqueConstraint("branch_id", "position", name="uq_execution_step_position"),
        CheckConstraint("position >= 0", name="ck_execution_step_position"),
        CheckConstraint(
            "(kind = 'function_call' AND function_call_id IS NOT NULL "
            "AND stack_snapshot_id IS NULL) OR "
            "(kind = 'stack_snapshot' AND stack_snapshot_id IS NOT NULL "
            "AND function_call_id IS NULL)",
            name="ck_execution_step_payload",
        ),
        CheckConstraint(
            "source_step_id IS NULL OR source_step_id <> id",
            name="ck_execution_step_not_own_source",
        ),
    )


class ExternalInteractionOccurrence(Base):
    """One external function call made while executing a step.

    The referenced :class:`FunctionCall` is the low-level VM observation.  This
    association adds only the high-level facts needed for deterministic replay:
    which step owns the interaction and its order among that step's external
    interactions.  What functions should be tracked and how they should be
    replayed are runtime/interface concerns and are intentionally not persisted
    here.

    For function-call steps, the referenced call can be any dynamically nested
    descendant of the step's function call; it need not be an immediate callee.
    For stack-snapshot steps, it occurred in the execution interval beginning at
    that snapshot and ending at the next step.
    """

    __tablename__ = "external_interaction_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("execution_steps.id"), nullable=False, index=True
    )
    function_call_id: Mapped[int] = mapped_column(
        ForeignKey("function_calls.id"), nullable=False, unique=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    step: Mapped[ExecutionStep] = relationship(
        back_populates="external_interactions"
    )
    function_call: Mapped[FunctionCall] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "position",
            name="uq_external_interaction_position",
        ),
        CheckConstraint(
            "position >= 0",
            name="ck_external_interaction_position",
        ),
    )


__all__ = [
    "Base",
    "CodeDefinition",
    "CodeObjectLink",
    "ExecutionBranch",
    "ExecutionSession",
    "ExecutionStatus",
    "ExecutionStep",
    "ExternalInteractionOccurrence",
    "FunctionCall",
    "FunctionCallCapturePerformance",
    "FunctionCallOutcome",
    "ObjectIdentity",
    "StackSnapshot",
    "StepKind",
    "StoredObject",
]
