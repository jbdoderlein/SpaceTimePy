"""Branch-fork and replay orchestration for SpaceTime integrations.

This layer deliberately does not mutate live Python frames. A replay caller
receives the exact stored state and ordered external-interaction script,
performs its own code/data migration, and executes the restarted code while the
monitor records the new branch.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from spacetimepy.core.model import (
    ExecutionBranch,
    ExecutionStatus,
    ExecutionStep,
    FunctionCallOutcome,
    utc_now,
)
from spacetimepy.interface.alignment import (
    AlignmentLink,
    AlignmentRelation,
    AlignmentResult,
    AlignmentService,
    OnlineAlignmentRun,
)
from spacetimepy.interface.data import (
    BranchDTO,
    ExternalInteractionDTO,
    StepDTO,
    TraceConsistencyError,
    TraceData,
    TraceNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping
    from types import FrameType

    from sqlalchemy.orm import Session

    from spacetimepy.core.monitoring import SpaceTimeMonitor
    from spacetimepy.interface.alignment import OnlineAlignmentAlgorithm


ReplayValue = TypeVar("ReplayValue")


class ReplayError(RuntimeError):
    """Base error raised by replay orchestration."""


class ReplayDivergenceError(ReplayError):
    """Raised when replayed external calls differ from the recorded order."""


@dataclass(frozen=True, slots=True)
class ReplayAlignmentPolicy:
    """Select optional online alignment for one replay.

    The default policy has no algorithm and leaves replay behavior unchanged.
    """

    algorithm: str | OnlineAlignmentAlgorithm | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "options",
            MappingProxyType(dict(self.options)),
        )

    @property
    def enabled(self) -> bool:
        return self.algorithm is not None


@dataclass(frozen=True, slots=True)
class RecordedExternalInteraction:
    """One materialized external interaction available to replay."""

    occurrence: ExternalInteractionDTO
    arguments: dict[str, Any]
    globals: dict[str, Any]
    return_value: Any
    exception: Any

    @property
    def outcome(self) -> str:
        return self.occurrence.call.outcome


@dataclass(slots=True)
class _ExternalInteractionState:
    interactions: tuple[RecordedExternalInteraction, ...]
    position: int = 0
    error: BaseException | None = None


class ExternalInteractionScript:
    """Consume recorded external results in their original call order."""

    def __init__(
        self, interactions: tuple[RecordedExternalInteraction, ...]
    ) -> None:
        self._state = _ExternalInteractionState(interactions)
        self._step_states: dict[int, _ExternalInteractionState] = {}

    @property
    def remaining(self) -> int:
        return len(self._state.interactions) - self._state.position

    def peek(self) -> RecordedExternalInteraction | None:
        if self._state.position >= len(self._state.interactions):
            return None
        return self._state.interactions[self._state.position]

    def take(self, target: Callable[..., Any] | str) -> Any:
        """Return/raise the next recorded outcome after validating its name."""

        if self._state.error is not None:
            raise ReplayDivergenceError(
                "External interaction replay is unavailable because online "
                "alignment failed"
            ) from self._state.error

        interaction = self.peek()
        if interaction is None:
            raise ReplayDivergenceError(
                f"Unexpected external call {self._target_label(target)!r}; "
                "the recorded script is exhausted"
            )

        recorded_names = self._recorded_names(interaction)
        target_names = self._target_names(target)
        if recorded_names.isdisjoint(target_names):
            expected = interaction.occurrence.call.qualified_name or (
                interaction.occurrence.call.function_name
            )
            raise ReplayDivergenceError(
                f"Expected external call {expected!r}, got "
                f"{self._target_label(target)!r}"
            )

        self._state.position += 1
        if interaction.outcome == FunctionCallOutcome.RAISED.value:
            if isinstance(interaction.exception, BaseException):
                raise interaction.exception
            raise ReplayDivergenceError(
                "The recorded external call raised an exception that could "
                "not be materialized as a BaseException"
            )
        if interaction.outcome != FunctionCallOutcome.RETURNED.value:
            raise ReplayDivergenceError(
                f"Recorded external call has non-replayable outcome "
                f"{interaction.outcome!r}"
            )
        return interaction.return_value

    def select(
        self,
        interactions: tuple[RecordedExternalInteraction, ...],
        *,
        step_id: int | None = None,
        require_consumed: bool = False,
    ) -> None:
        """Select the recorded interactions for the next aligned step."""

        if require_consumed:
            self.assert_consumed()
        if step_id is None:
            self._state = _ExternalInteractionState(interactions)
            return
        self._state = self._step_states.setdefault(
            step_id,
            _ExternalInteractionState(interactions),
        )

    def fail(self, error: BaseException) -> None:
        """Prevent consumption after an online alignment failure."""

        self._state.interactions = ()
        self._state.position = 0
        self._state.error = error

    def mock(self, target: Callable[..., Any]) -> Callable[..., Any]:
        """Build a simple replacement callable backed by this script."""

        @wraps(target)
        def replacement(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return self.take(target)

        return replacement

    def assert_consumed(self) -> None:
        states = (
            tuple(self._step_states.values())
            if self._step_states
            else (self._state,)
        )
        for state in states:
            if state.error is not None:
                raise ReplayDivergenceError(
                    "External interaction replay is unavailable because online "
                    "alignment failed"
                ) from state.error
        remaining = sum(
            len(state.interactions) - state.position for state in states
        )
        if remaining:
            interaction = next(
                (
                    state.interactions[state.position]
                    for state in states
                    if state.position < len(state.interactions)
                ),
                None,
            )
            name = (
                interaction.occurrence.call.qualified_name
                if interaction is not None
                else None
            )
            raise ReplayDivergenceError(
                f"Replay finished with {remaining} external interaction(s) "
                f"remaining; next is {name!r}"
            )

    @staticmethod
    def _recorded_names(
        interaction: RecordedExternalInteraction,
    ) -> set[str]:
        call = interaction.occurrence.call
        names = {call.function_name}
        replay_target_names = call.attributes.get("replay_target_names", ())
        if isinstance(replay_target_names, (list, tuple)):
            names.update(
                name for name in replay_target_names if isinstance(name, str)
            )
        if call.qualified_name:
            names.add(call.qualified_name)
        if call.module_name:
            names.add(f"{call.module_name}.{call.function_name}")
            if call.qualified_name:
                names.add(f"{call.module_name}.{call.qualified_name}")
        return names

    @staticmethod
    def _target_names(target: Callable[..., Any] | str) -> set[str]:
        if isinstance(target, str):
            return {target}
        names = {target.__name__, target.__qualname__}
        if target.__module__:
            names.add(f"{target.__module__}.{target.__name__}")
            names.add(f"{target.__module__}.{target.__qualname__}")
        return names

    @classmethod
    def _target_label(cls, target: Callable[..., Any] | str) -> str:
        if isinstance(target, str):
            return target
        return f"{target.__module__}.{target.__qualname__}"


@dataclass(slots=True)
class ReplayContext:
    """Migration input and runtime-only policy for one new branch."""

    branch_id: int
    session_id: int
    parent_branch_id: int
    forked_from_step: StepDTO
    locals: dict[str, Any]
    globals: dict[str, Any]
    external_interactions: tuple[RecordedExternalInteraction, ...]
    external: ExternalInteractionScript
    recipe: dict[str, Any]
    options: dict[str, Any]
    alignment_policy: ReplayAlignmentPolicy
    alignment: AlignmentResult | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    branch: BranchDTO
    value: Any
    alignment: AlignmentResult | None = None


class ReplayInterface:
    """Create and record branches restarted from an exact prior step."""

    def __init__(
        self,
        database: Session,
        monitor: SpaceTimeMonitor,
        data: TraceData,
        alignment: AlignmentService | None = None,
    ) -> None:
        self._database = database
        self._monitor = monitor
        self._data = data
        self._alignment_service = alignment or AlignmentService(data)
        self._active_context: ReplayContext | None = None
        self._active_alignment: OnlineAlignmentRun | None = None
        self._active_alignment_error: BaseException | None = None

    @property
    def active_session_id(self) -> int | None:
        """Return the session currently owned by the live recorder, if any."""

        session = self._monitor.current_session
        return session.id if session is not None else None

    def prepare(
        self,
        *,
        parent_branch_id: int,
        forked_from_step_id: int,
        recipe: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        alignment_policy: ReplayAlignmentPolicy | None = None,
    ) -> ReplayContext:
        """Materialize replay input without creating or recording a branch."""

        parent = self._require_parent_and_source(
            parent_branch_id, forked_from_step_id
        )[0]
        return self._build_context(
            branch_id=-1,
            session_id=parent.session_id,
            parent_branch_id=parent_branch_id,
            forked_from_step_id=forked_from_step_id,
            recipe=recipe,
            options=options,
            alignment_policy=alignment_policy,
        )

    def begin(
        self,
        *,
        parent_branch_id: int,
        forked_from_step_id: int,
        name: str | None = None,
        configuration_key: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        alignment_policy: ReplayAlignmentPolicy | None = None,
    ) -> ReplayContext:
        """Create a child branch and attach the monitor before execution."""

        if self._monitor.current_branch is not None or self._active_context is not None:
            raise ReplayError("A SpaceTime recording or replay is already active")

        parent, source = self._require_parent_and_source(
            parent_branch_id, forked_from_step_id
        )
        context = self._build_context(
            branch_id=-1,
            session_id=parent.session_id,
            parent_branch_id=parent_branch_id,
            forked_from_step_id=forked_from_step_id,
            recipe=recipe,
            options=options,
            alignment_policy=alignment_policy,
        )
        branch = ExecutionBranch(
            session=parent.session,
            parent_branch=parent,
            forked_from_step=source,
            name=name,
            status=ExecutionStatus.OPEN,
            configuration_key=configuration_key,
            recipe=dict(recipe or {}),
            attributes=dict(attributes or {}),
        )
        self._database.add(branch)

        # Fork creation is cold path.  Stable IDs make the context safe to
        # hand to another integration component before execution begins.
        self._database.flush()
        if branch.id is None:
            raise ReplayError("The database did not assign a replay branch ID")

        context.branch_id = branch.id
        try:
            self._start_alignment(context)
            self._monitor.start_branch(branch)
        except BaseException:
            self._discard_alignment()
            self._database.delete(branch)
            self._database.flush()
            raise
        self._active_context = context
        return context

    def begin_active_execution(
        self,
        *,
        source_frame: FrameType,
        replacement_frame: FrameType,
        replacement_target: Callable[..., Any],
        replacement_line_numbers: Collection[int] | None = None,
        parent_branch_id: int | None = None,
        forked_from_step_id: int | None = None,
        name: str | None = None,
        configuration_key: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        alignment_policy: ReplayAlignmentPolicy | None = None,
    ) -> ReplayContext:
        """Fork an active recording while an integration replaces its frame.

        SpaceTimePy transfers recorder ownership but does not mutate either
        frame.  The integration must create the replacement frame, migrate its
        state, and redirect execution.
        """

        active_branch = self._monitor.current_branch
        execution_session = self._monitor.current_session
        if active_branch is None or execution_session is None:
            raise ReplayError("No SpaceTime recording is active")
        if (
            self._active_context is not None
            and self._active_context.branch_id != active_branch.id
        ):
            raise ReplayError("Another SpaceTime replay is already active")
        if active_branch.id is None:
            self._monitor.flush()
        if active_branch.id is None:
            raise ReplayError("The active branch has no persistent ID")

        if parent_branch_id is None:
            active_step = self._monitor.active_step_for_frame(source_frame)
            if active_step is None or active_step.id is None:
                self._monitor.flush()
                active_step = self._monitor.active_step_for_frame(source_frame)
            if active_step is None or active_step.id is None:
                raise ReplayError("The active frame has no recorded step to fork")
            selected_step_id = forked_from_step_id or active_step.id
            if selected_step_id != active_step.id:
                raise ReplayError(
                    "A historical active fork requires its parent branch ID"
                )
            parent = active_branch
            source = active_step
        else:
            if forked_from_step_id is None:
                raise ReplayError(
                    "A historical active fork requires its forked-from step ID"
                )
            parent, source = self._require_parent_and_source(
                parent_branch_id,
                forked_from_step_id,
            )
            if parent.session_id != execution_session.id:
                raise ReplayError(
                    "An active execution can only explore a checkpoint from "
                    "its current session"
                )
            selected_step_id = forked_from_step_id

        context = self._build_context(
            branch_id=-1,
            session_id=execution_session.id,
            parent_branch_id=parent.id,
            forked_from_step_id=selected_step_id,
            recipe=recipe,
            options=options,
            alignment_policy=alignment_policy,
        )
        branch = ExecutionBranch(
            session=execution_session,
            parent_branch=parent,
            forked_from_step=source,
            name=name,
            status=ExecutionStatus.OPEN,
            configuration_key=configuration_key,
            recipe=dict(recipe or {}),
            attributes=dict(attributes or {}),
        )
        self._database.add(branch)
        self._database.flush()
        if branch.id is None:
            raise ReplayError("The database did not assign an active replay branch ID")

        context.branch_id = branch.id
        if self._active_context is not None:
            self._finish_alignment(self._active_context)
        try:
            self._start_alignment(context)
            self._monitor.replace_active_execution(
                branch,
                source_frame=source_frame,
                replacement_frame=replacement_frame,
                replacement_target=replacement_target,
                replacement_line_numbers=replacement_line_numbers,
            )
        except BaseException:
            self._discard_alignment()
            self._database.delete(branch)
            self._database.flush()
            raise
        self._active_context = context
        return context

    def record_active_replacement_state(
        self,
        *,
        frame: FrameType,
        line_number: int,
        replacement_line_numbers: Collection[int] | None = None,
    ) -> None:
        """Record restored replacement locals at their debugger-mapped line.

        This is the second half of :meth:`begin_active_execution` for
        integrations that must enter replacement code through an earlier
        trampoline or breakpoint before redirecting execution.
        """

        context = self._active_context
        branch = self._monitor.current_branch
        if context is None or branch is None:
            raise ReplayError("No SpaceTime replay is active")
        if branch.id != context.branch_id:
            raise ReplayError("The active monitor branch is not this replay")

        self._monitor.record_active_replacement_state(
            frame,
            line_number,
            line_numbers=replacement_line_numbers,
        )
        self._monitor.flush()

    def finish(
        self,
        status: str = "completed",
        *,
        commit: bool = True,
    ) -> BranchDTO:
        """Finish the replay currently attached to the monitor."""

        context = self._active_context
        branch = self._monitor.current_branch
        execution_session = self._monitor.current_session
        if context is None or branch is None or execution_session is None:
            raise ReplayError("No SpaceTime replay is active")
        if branch.id != context.branch_id:
            raise ReplayError("The active monitor branch is not this replay")

        selected_status = ExecutionStatus(status)
        alignment_error: BaseException | None = None
        if selected_status == ExecutionStatus.COMPLETED:
            try:
                self._finish_alignment(context)
            except BaseException as error:
                alignment_error = error
                selected_status = ExecutionStatus.FAILED
        else:
            self._discard_alignment()

        self._monitor.finish_branch(selected_status, commit=False)
        execution_session.status = selected_status
        execution_session.completed_at = (
            None if selected_status == ExecutionStatus.OPEN else utc_now()
        )
        self._database.flush()
        self._active_context = None

        if selected_status == ExecutionStatus.COMPLETED and not branch.steps:
            branch.status = ExecutionStatus.FAILED
            branch.completed_at = utc_now()
            execution_session.status = ExecutionStatus.FAILED
            execution_session.completed_at = branch.completed_at
            self._database.flush()
            if commit:
                self._database.commit()
            raise ReplayError(
                "A completed replay must record a first replacement step"
            )

        if commit:
            self._database.commit()
        if alignment_error is not None:
            raise ReplayError("Online replay alignment failed") from alignment_error
        return self._data.get_branch(context.branch_id)

    @contextmanager
    def replaying(
        self,
        *,
        parent_branch_id: int,
        forked_from_step_id: int,
        name: str | None = None,
        configuration_key: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        alignment_policy: ReplayAlignmentPolicy | None = None,
        require_external_consumption: bool = False,
        commit: bool = True,
    ) -> Iterator[ReplayContext]:
        """Manage a replay branch around integration-specific execution."""

        context = self.begin(
            parent_branch_id=parent_branch_id,
            forked_from_step_id=forked_from_step_id,
            name=name,
            configuration_key=configuration_key,
            recipe=recipe,
            attributes=attributes,
            options=options,
            alignment_policy=alignment_policy,
        )
        try:
            yield context
            if require_external_consumption:
                context.external.assert_consumed()
        except BaseException:
            self.finish(ExecutionStatus.FAILED.value, commit=commit)
            raise
        else:
            self.finish(ExecutionStatus.COMPLETED.value, commit=commit)

    def run(
        self,
        executor: Callable[[ReplayContext], ReplayValue],
        *,
        parent_branch_id: int,
        forked_from_step_id: int,
        name: str | None = None,
        configuration_key: str | None = None,
        recipe: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        alignment_policy: ReplayAlignmentPolicy | None = None,
        require_external_consumption: bool = False,
        commit: bool = True,
    ) -> ReplayResult:
        """Execute a migration/restart callback while recording a new branch."""

        context: ReplayContext
        value: ReplayValue
        with self.replaying(
            parent_branch_id=parent_branch_id,
            forked_from_step_id=forked_from_step_id,
            name=name,
            configuration_key=configuration_key,
            recipe=recipe,
            attributes=attributes,
            options=options,
            alignment_policy=alignment_policy,
            require_external_consumption=require_external_consumption,
            commit=commit,
        ) as context:
            value = executor(context)

        return ReplayResult(
            branch=self._data.get_branch(context.branch_id),
            value=value,
            alignment=context.alignment,
        )

    def _build_context(
        self,
        *,
        branch_id: int,
        session_id: int,
        parent_branch_id: int,
        forked_from_step_id: int,
        recipe: Mapping[str, Any] | None,
        options: Mapping[str, Any] | None,
        alignment_policy: ReplayAlignmentPolicy | None,
    ) -> ReplayContext:
        source = self._data.get_step(forked_from_step_id)
        if source.stack_snapshot is not None:
            local_refs = source.stack_snapshot.local_references
            global_refs = source.stack_snapshot.global_references
        elif source.function_call is not None:
            local_refs = source.function_call.entry_local_references
            global_refs = source.function_call.entry_global_references
        else:
            raise TraceConsistencyError(
                f"Step {forked_from_step_id} has no VM payload"
            )

        references = [*local_refs.values(), *global_refs.values()]
        for occurrence in source.external_interactions:
            call = occurrence.call
            references.extend(call.entry_local_references.values())
            references.extend(call.entry_global_references.values())
            if call.return_reference is not None:
                references.append(call.return_reference)
            if call.exception_reference is not None:
                references.append(call.exception_reference)
        loaded_values = self._data.load_values(references)

        interactions = tuple(
            self._materialize_external(occurrence, loaded_values)
            for occurrence in source.external_interactions
        )
        return ReplayContext(
            branch_id=branch_id,
            session_id=session_id,
            parent_branch_id=parent_branch_id,
            forked_from_step=source,
            locals=self._materialize_references(local_refs, loaded_values),
            globals=self._materialize_references(global_refs, loaded_values),
            external_interactions=interactions,
            external=ExternalInteractionScript(interactions),
            recipe=dict(recipe or {}),
            options=dict(options or {}),
            alignment_policy=alignment_policy or ReplayAlignmentPolicy(),
        )

    def _start_alignment(
        self,
        context: ReplayContext,
    ) -> None:
        policy = context.alignment_policy
        if not policy.enabled:
            return
        self._active_alignment = self._alignment_service.start_online(
            reference_branch_id=context.parent_branch_id,
            target_branch_id=context.branch_id,
            algorithm=policy.algorithm,
            options=policy.options,
        )
        self._active_alignment_error = None
        self._monitor.add_step_activation_listener(self._activate_recorded_step)

    def _activate_recorded_step(
        self,
        monitor: SpaceTimeMonitor,
        step: ExecutionStep,
    ) -> None:
        context = self._active_context
        alignment = self._active_alignment
        if context is None or alignment is None or step.branch.id != context.branch_id:
            return

        if self._active_alignment_error is not None:
            context.external.fail(self._active_alignment_error)
            return

        try:
            monitor.flush()
            if step.id is None:
                raise ReplayError("The new replay step has no persistent ID")
            target_step = self._data.get_step(step.id)
            link = alignment.link_for_target(target_step)
            if link is None:
                links = alignment.align(target_step)
                target_links = tuple(
                    candidate
                    for candidate in links
                    if candidate.target_step is not None
                    and candidate.target_step.id == target_step.id
                )
                if len(target_links) != 1:
                    raise ReplayDivergenceError(
                        "An online replay algorithm must immediately return "
                        "exactly one link for target step "
                        f"{target_step.id}"
                    )
                link = target_links[0]
            interactions = self._interactions_for_alignment_link(link)
            context.external.select(
                interactions,
                step_id=target_step.id,
            )
            context.external_interactions = interactions
        except BaseException as error:
            self._active_alignment_error = error
            context.external.fail(error)

    def _finish_alignment(self, context: ReplayContext) -> None:
        alignment = self._active_alignment
        self._monitor.remove_step_activation_listener(
            self._activate_recorded_step
        )
        try:
            if alignment is None:
                return
            if self._active_alignment_error is not None:
                raise self._active_alignment_error
            self._monitor.flush()
            alignment.finish()
            context.alignment = alignment.result
        finally:
            self._clear_alignment_state()

    def _discard_alignment(self) -> None:
        self._monitor.remove_step_activation_listener(
            self._activate_recorded_step
        )
        self._clear_alignment_state()

    def _clear_alignment_state(self) -> None:
        self._active_alignment = None
        self._active_alignment_error = None

    def _interactions_for_alignment_link(
        self,
        link: AlignmentLink,
    ) -> tuple[RecordedExternalInteraction, ...]:
        if link.relation == AlignmentRelation.INSERTED:
            return ()
        if link.reference_step is None:
            raise ReplayDivergenceError(
                f"{link.relation.value} link has no reference step"
            )
        return self._materialize_step_interactions(link.reference_step)

    def _materialize_step_interactions(
        self,
        source: StepDTO,
    ) -> tuple[RecordedExternalInteraction, ...]:
        references: list[str] = []
        for occurrence in source.external_interactions:
            call = occurrence.call
            references.extend(call.entry_local_references.values())
            references.extend(call.entry_global_references.values())
            if call.return_reference is not None:
                references.append(call.return_reference)
            if call.exception_reference is not None:
                references.append(call.exception_reference)
        loaded_values = self._data.load_values(references)
        return tuple(
            self._materialize_external(occurrence, loaded_values)
            for occurrence in source.external_interactions
        )

    def _materialize_external(
        self,
        occurrence: ExternalInteractionDTO,
        loaded_values: dict[str, Any],
    ) -> RecordedExternalInteraction:
        call = occurrence.call
        return RecordedExternalInteraction(
            occurrence=occurrence,
            arguments=self._materialize_references(
                call.entry_local_references, loaded_values
            ),
            globals=self._materialize_references(
                call.entry_global_references, loaded_values
            ),
            return_value=(
                loaded_values[call.return_reference]
                if call.return_reference is not None
                else None
            ),
            exception=(
                loaded_values[call.exception_reference]
                if call.exception_reference is not None
                else None
            ),
        )

    @staticmethod
    def _materialize_references(
        references: dict[str, str],
        loaded_values: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            name: loaded_values[reference]
            for name, reference in references.items()
        }

    def _require_parent_and_source(
        self,
        parent_branch_id: int,
        forked_from_step_id: int,
    ) -> tuple[ExecutionBranch, ExecutionStep]:
        parent = self._database.get(ExecutionBranch, parent_branch_id)
        if parent is None:
            raise TraceNotFoundError(
                f"No execution branch with id {parent_branch_id!r}"
            )
        source = self._database.get(ExecutionStep, forked_from_step_id)
        if source is None:
            raise TraceNotFoundError(
                f"No execution step with id {forked_from_step_id!r}"
            )
        if forked_from_step_id not in self._data.resolve_branch_step_ids(
            parent_branch_id
        ):
            raise ReplayError(
                f"Step {forked_from_step_id} is not in the resolved path of "
                f"branch {parent_branch_id}"
            )
        if source.kind != parent.session.step_kind:
            raise TraceConsistencyError(
                "The fork step kind differs from the execution session"
            )
        return parent, source


__all__ = [
    "ExternalInteractionScript",
    "RecordedExternalInteraction",
    "ReplayAlignmentPolicy",
    "ReplayContext",
    "ReplayDivergenceError",
    "ReplayError",
    "ReplayInterface",
    "ReplayResult",
]
