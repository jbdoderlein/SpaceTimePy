"""Low-level VM recorder for the next SpaceTimePy model.

This module deliberately contains no decorator or persistent capture
declaration.  The public interface layer is responsible for deciding:

* which code objects receive ``sys.monitoring`` events;
* whether a call is a timeline step, supporting VM call, or external
  interaction;
* which line events and variables should be captured;
* which hooks or replay policies should run.

The interface registers those decisions on the singleton.  The monitor owns
the ``sys.monitoring`` tool, callbacks, event routing, and the VM-facing state
required while a program is executing.  It writes directly to the SQLAlchemy
models, but does not create/export databases, own the ORM session lifecycle,
query through a repository, or use an ``ObjectManager``.

The ``record_*`` methods remain public so the event boundary can be tested
without executing monitored code.  A later interface adapter only needs to
translate decorators into transient calls to ``register_capture``.
"""

from __future__ import annotations

import datetime
import dis
import hashlib
import inspect
import json
import logging
import sys
import types
import uuid
from collections.abc import Callable, Collection, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from .model import (
    CodeDefinition,
    ExecutionBranch,
    ExecutionSession,
    ExecutionStatus,
    ExecutionStep,
    ExternalInteractionOccurrence,
    FunctionCall,
    FunctionCallOutcome,
    ObjectIdentity,
    StackSnapshot,
    StepKind,
    StoredObject,
)
from .serialization import PickleSerializer

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class MonitoringStateError(RuntimeError):
    """Raised when VM events cannot form a valid trace."""


class CallRole(StrEnum):
    """Transient instruction supplied by the capture interface for a call.

    The role is not persisted.  The resulting models contain all information
    needed to understand what was recorded.
    """

    STEP = "step"
    SUPPORT = "support"
    EXTERNAL_INTERACTION = "external_interaction"


StartAttributes = Callable[
    ["SpaceTimeMonitor", types.FrameType, types.CodeType, int],
    Mapping[str, Any] | None,
]
ReturnAttributes = Callable[
    ["SpaceTimeMonitor", types.FrameType, types.CodeType, int, Any],
    Mapping[str, Any] | None,
]
LineAttributes = Callable[
    ["SpaceTimeMonitor", types.FrameType, types.CodeType, int],
    Mapping[str, Any] | None,
]


@dataclass(frozen=True, slots=True)
class CaptureRegistration:
    """Transient capture instruction installed by the interface layer."""

    code: types.CodeType
    role: CallRole
    capture_lines: bool = False
    line_numbers: frozenset[int] | None = None
    ignored_names: frozenset[str] = frozenset()
    start_attributes: StartAttributes | None = None
    return_attributes: ReturnAttributes | None = None
    line_attributes: LineAttributes | None = None


@dataclass
class _ActiveCall:
    frame_id: int
    code: types.CodeType
    function_call: FunctionCall
    role: CallRole
    current_step: ExecutionStep | None = None


class SpaceTimeMonitor:
    """Singleton recorder attached to one Python VM and one ORM session.

    The singleton may record several branches sequentially.  An ORM session is
    injected because database creation, export, and disposal belong to the
    programmatic interface rather than the VM recorder.

    This first version assumes events are delivered from one execution context
    at a time, as in the existing interactive integrations.
    Supporting simultaneous threads will require context-local call stacks and
    ORM sessions, without changing the trace model.
    """

    _instance: ClassVar[SpaceTimeMonitor | None] = None

    def __new__(
        cls,
        database: Session,
        *,
        tool_id: int = sys.monitoring.PROFILER_ID,
        flush_batch_size: int = 256,
        serializer: PickleSerializer | None = None,
    ) -> SpaceTimeMonitor:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        database: Session,
        *,
        tool_id: int = sys.monitoring.PROFILER_ID,
        flush_batch_size: int = 256,
        serializer: PickleSerializer | None = None,
    ) -> None:
        if getattr(self, "_initialized", False):
            if self.database is not database:
                raise MonitoringStateError(
                    "SpaceTimeMonitor is already initialized with another ORM session"
                )
            if serializer is not None and self.serializer is not serializer:
                raise MonitoringStateError(
                    "SpaceTimeMonitor is already initialized with another serializer"
                )
            return
        if flush_batch_size < 0:
            raise ValueError("flush_batch_size must be zero or positive")

        self._initialized = True
        self.database = database
        self.tool_id = tool_id
        self.flush_batch_size = flush_batch_size
        self.serializer = serializer or PickleSerializer()
        self.is_recording_enabled = True
        self.last_callback_error: BaseException | None = None

        self.current_session: ExecutionSession | None = None
        self.current_branch: ExecutionBranch | None = None

        self._captures: dict[types.CodeType, CaptureRegistration] = {}
        self._active_calls: list[_ActiveCall] = []
        self._active_calls_by_frame: dict[int, _ActiveCall] = {}
        self._next_step_position = 0
        self._next_external_positions: dict[int, int] = {}
        self._pending_events = 0
        self._inside_callback = False

        self._code_definition_cache: dict[types.CodeType, str | None] = {}
        self._known_code_definition_ids: set[str] = set()
        self._global_names_cache: dict[types.CodeType, frozenset[str]] = {}
        self._identity_cache: dict[str, ObjectIdentity] = {}
        self._stored_value_cache: dict[tuple[str, str], str] = {}
        self._next_object_versions: dict[str, int] = {}
        self._identity_namespace = uuid.uuid4().hex

        self._install_monitoring_tool()

    @classmethod
    def get_instance(cls) -> SpaceTimeMonitor | None:
        return cls._instance

    @property
    def active_calls(self) -> tuple[FunctionCall, ...]:
        """Return the captured calls currently active in the VM."""

        return tuple(active.function_call for active in self._active_calls)

    @property
    def captures(self) -> tuple[CaptureRegistration, ...]:
        """Return the transient capture registrations currently installed."""

        return tuple(self._captures.values())

    def register_capture(
        self,
        target: types.CodeType | Callable[..., Any],
        *,
        role: CallRole = CallRole.STEP,
        capture_lines: bool = False,
        line_numbers: Collection[int] | None = None,
        ignored_names: Collection[str] = (),
        start_attributes: StartAttributes | None = None,
        return_attributes: ReturnAttributes | None = None,
        line_attributes: LineAttributes | None = None,
    ) -> CaptureRegistration:
        """Install an in-memory capture instruction and enable its VM events.

    Decorators in the public interface layer call this method.  No
        registration is written to the trace database.
        """

        code = self._code_from_target(target)
        role = CallRole(role)
        selected_lines = (
            None if line_numbers is None else frozenset(line_numbers)
        )
        capture_lines = capture_lines or selected_lines is not None
        if role == CallRole.EXTERNAL_INTERACTION and capture_lines:
            raise ValueError("External interaction captures cannot include line events")

        registration = CaptureRegistration(
            code=code,
            role=role,
            capture_lines=capture_lines,
            line_numbers=selected_lines,
            ignored_names=frozenset(ignored_names),
            start_attributes=start_attributes,
            return_attributes=return_attributes,
            line_attributes=line_attributes,
        )
        self._captures[code] = registration

        # Source inspection and bytecode analysis are registration-time work,
        # not costs to pay on the first monitored invocation.
        self._store_code_definition(code)
        self._global_names_for_code(code)

        events = (
            sys.monitoring.events.PY_START
            | sys.monitoring.events.PY_RETURN
        )
        if capture_lines:
            events |= sys.monitoring.events.LINE
        sys.monitoring.set_local_events(self.tool_id, code, events)
        self._refresh_global_events()
        return registration

    def unregister_capture(
        self,
        target: types.CodeType | Callable[..., Any],
    ) -> CaptureRegistration | None:
        """Disable VM events and remove one transient capture instruction."""

        code = self._code_from_target(target)
        sys.monitoring.set_local_events(self.tool_id, code, 0)
        registration = self._captures.pop(code, None)
        self._refresh_global_events()
        return registration

    def clear_captures(self) -> None:
        """Disable every code-local event registered on this monitor."""

        for code in tuple(self._captures):
            sys.monitoring.set_local_events(self.tool_id, code, 0)
        self._captures.clear()
        self._refresh_global_events()

    def _refresh_global_events(self) -> None:
        # PY_UNWIND is not a valid code-local event.  It is rare compared with
        # starts and lines; the callback rejects untracked frames in O(1).
        events = sys.monitoring.events.PY_UNWIND if self._captures else 0
        sys.monitoring.set_events(self.tool_id, events)

    def _install_monitoring_tool(self) -> None:
        tool_name = sys.monitoring.get_tool(self.tool_id)
        if tool_name is None:
            sys.monitoring.use_tool_id(self.tool_id, "spacetimepy")
        elif tool_name != "spacetimepy":
            raise MonitoringStateError(
                f"sys.monitoring tool ID {self.tool_id} is already used by {tool_name!r}"
            )

        sys.monitoring.set_events(self.tool_id, 0)
        sys.monitoring.register_callback(
            self.tool_id,
            sys.monitoring.events.PY_START,
            self._monitor_callback_function_start,
        )
        sys.monitoring.register_callback(
            self.tool_id,
            sys.monitoring.events.PY_RETURN,
            self._monitor_callback_function_return,
        )
        sys.monitoring.register_callback(
            self.tool_id,
            sys.monitoring.events.PY_UNWIND,
            self._monitor_callback_function_unwind,
        )
        sys.monitoring.register_callback(
            self.tool_id,
            sys.monitoring.events.LINE,
            self._monitor_callback_line,
        )

    def _uninstall_monitoring_tool(self) -> None:
        self.clear_captures()
        sys.monitoring.set_events(self.tool_id, 0)
        for event in (
            sys.monitoring.events.PY_START,
            sys.monitoring.events.PY_RETURN,
            sys.monitoring.events.PY_UNWIND,
            sys.monitoring.events.LINE,
        ):
            sys.monitoring.register_callback(self.tool_id, event, None)
        if sys.monitoring.get_tool(self.tool_id) == "spacetimepy":
            sys.monitoring.free_tool_id(self.tool_id)

    def _monitor_callback_function_start(
        self,
        code: types.CodeType,
        instruction_offset: int,
    ) -> None:
        registration = self._captures.get(code)
        if (
            registration is None
            or not self.is_recording_enabled
            or self.current_branch is None
            or self._inside_callback
        ):
            return

        callback_frame = inspect.currentframe()
        frame = callback_frame.f_back if callback_frame is not None else None
        del callback_frame
        if frame is None:
            return
        if (
            registration.role == CallRole.EXTERNAL_INTERACTION
            and self._nearest_owner_step(frame.f_back) is None
        ):
            return

        self._inside_callback = True
        try:
            attributes = self._call_start_attributes(
                registration, frame, instruction_offset
            )
            self.record_function_start(
                frame,
                code=code,
                instruction_offset=instruction_offset,
                role=registration.role,
                ignored_names=registration.ignored_names,
                attributes=attributes,
            )
        except BaseException as error:  # callbacks must not alter user execution
            self._handle_callback_error(error)
        finally:
            self._inside_callback = False

    def _monitor_callback_function_return(
        self,
        code: types.CodeType,
        instruction_offset: int,
        return_value: Any,
    ) -> None:
        if self._inside_callback:
            return

        callback_frame = inspect.currentframe()
        frame = callback_frame.f_back if callback_frame is not None else None
        del callback_frame
        if frame is None or id(frame) not in self._active_calls_by_frame:
            return

        self._inside_callback = True
        try:
            registration = self._captures.get(code)
            attributes = self._call_return_attributes(
                registration,
                frame,
                code,
                instruction_offset,
                return_value,
            )
            self.record_function_return(
                frame,
                return_value,
                attributes=attributes,
            )
        except BaseException as error:  # callbacks must not alter user execution
            self._handle_callback_error(error)
        finally:
            self._inside_callback = False

    def _monitor_callback_function_unwind(
        self,
        code: types.CodeType,
        instruction_offset: int,
        exception: BaseException,
    ) -> None:
        if self._inside_callback:
            return

        callback_frame = inspect.currentframe()
        frame = callback_frame.f_back if callback_frame is not None else None
        del callback_frame
        if frame is None or id(frame) not in self._active_calls_by_frame:
            return

        self._inside_callback = True
        try:
            registration = self._captures.get(code)
            attributes = self._call_return_attributes(
                registration,
                frame,
                code,
                instruction_offset,
                exception,
            )
            self.record_function_unwind(
                frame,
                exception,
                attributes=attributes,
            )
        except BaseException as error:  # callbacks must not alter user execution
            self._handle_callback_error(error)
        finally:
            self._inside_callback = False

    def _monitor_callback_line(
        self,
        code: types.CodeType,
        line_number: int,
    ) -> None:
        registration = self._captures.get(code)
        if (
            registration is None
            or not registration.capture_lines
            or not self.is_recording_enabled
            or self.current_branch is None
            or self._inside_callback
            or (
                registration.line_numbers is not None
                and line_number not in registration.line_numbers
            )
        ):
            return

        callback_frame = inspect.currentframe()
        frame = callback_frame.f_back if callback_frame is not None else None
        del callback_frame
        if frame is None:
            return

        self._inside_callback = True
        try:
            attributes = self._call_line_attributes(
                registration, frame, line_number
            )
            self.record_stack_snapshot(
                frame,
                line_number,
                code=code,
                ignored_names=registration.ignored_names,
                attributes=attributes,
            )
        except BaseException as error:  # callbacks must not alter user execution
            self._handle_callback_error(error)
        finally:
            self._inside_callback = False

    def start_branch(self, branch: ExecutionBranch) -> ExecutionBranch:
        """Attach the recorder to an existing or newly constructed branch.

        The branch must already reference its :class:`ExecutionSession`.  The
        interface is responsible for constructing the session, root/child
        branch, and fork relation before recording begins.
        """

        if self._active_calls:
            raise MonitoringStateError(
                "Cannot change branch while captured function calls are active"
            )
        if branch.session is None:
            raise MonitoringStateError("The branch must belong to an execution session")

        branch.status = ExecutionStatus.OPEN
        branch.completed_at = None
        if branch.session.status != ExecutionStatus.OPEN:
            branch.session.status = ExecutionStatus.OPEN
            branch.session.completed_at = None

        self.database.add(branch)
        last_position = max(
            (step.position for step in branch.steps),
            default=-1,
        )

        self.current_session = branch.session
        self.current_branch = branch
        self._next_step_position = last_position + 1
        self._next_external_positions.clear()
        return branch

    def finish_branch(
        self,
        status: ExecutionStatus = ExecutionStatus.COMPLETED,
        *,
        commit: bool = False,
    ) -> ExecutionBranch:
        """Finish the active branch without closing the injected ORM session."""

        branch = self._require_branch()
        if self._active_calls:
            names = ", ".join(
                active.function_call.qualified_name
                or active.function_call.function_name
                for active in self._active_calls
            )
            raise MonitoringStateError(
                f"Cannot finish branch with active calls: {names}"
            )

        branch.status = status
        branch.completed_at = (
            None if status == ExecutionStatus.OPEN else self._now()
        )
        self.database.flush()
        if commit:
            self.database.commit()

        self.current_branch = None
        self.current_session = None
        self._next_external_positions.clear()
        self._pending_events = 0
        return branch

    def enable_recording(self) -> None:
        self.is_recording_enabled = True

    def disable_recording(self) -> None:
        self.is_recording_enabled = False

    @contextmanager
    def recording_disabled(self):
        """Temporarily ignore new calls and line events."""

        was_enabled = self.is_recording_enabled
        self.is_recording_enabled = False
        try:
            yield
        finally:
            self.is_recording_enabled = was_enabled

    def flush(self) -> None:
        self.database.flush()
        self._pending_events = 0

    def commit(self) -> None:
        self.database.commit()
        self._pending_events = 0

    def rollback(self) -> None:
        self.database.rollback()
        self._pending_events = 0
        self._active_calls.clear()
        self._active_calls_by_frame.clear()

    def shutdown(self, *, commit: bool = False) -> None:
        """Detach the singleton without closing the interface-owned session."""

        self._uninstall_monitoring_tool()
        if self._active_calls:
            logger.warning(
                "Discarding %d active monitoring call(s) during shutdown",
                len(self._active_calls),
            )
        if commit:
            self.database.commit()
        else:
            self.database.flush()
        self._pending_events = 0

        self._active_calls.clear()
        self._active_calls_by_frame.clear()
        self.current_branch = None
        self.current_session = None
        self._initialized = False
        type(self)._instance = None

    def record_function_start(
        self,
        frame: types.FrameType,
        *,
        code: types.CodeType | None = None,
        instruction_offset: int | None = None,
        role: CallRole = CallRole.SUPPORT,
        ignored_names: Collection[str] = (),
        attributes: Mapping[str, Any] | None = None,
        source_step: ExecutionStep | None = None,
    ) -> FunctionCall | None:
        """Record a selected ``PY_START`` event.

        ``role`` is supplied by the interface and is intentionally transient:

        * ``STEP`` creates a function-call step, or enables snapshot steps for
          this frame, according to the active session's ``step_kind``;
        * ``SUPPORT`` stores only the VM call;
        * ``EXTERNAL_INTERACTION`` stores the VM call and appends an ordered
          occurrence to the currently executing step.
        """

        if not self.is_recording_enabled:
            return None
        execution_session = self._require_session()
        self._require_branch()

        code = code or frame.f_code
        role = CallRole(role)
        frame_id = id(frame)
        if frame_id in self._active_calls_by_frame:
            raise MonitoringStateError(
                f"Frame for {code.co_qualname} has already been recorded"
            )

        owner_step = None
        if role == CallRole.EXTERNAL_INTERACTION:
            owner_step = self._nearest_owner_step(frame.f_back)
            if owner_step is None:
                raise MonitoringStateError(
                    f"External interaction {code.co_qualname} occurred outside "
                    "an active execution step"
                )

        ignored = frozenset(ignored_names)
        local_refs, local_errors = self._capture_mapping(frame.f_locals, ignored)
        globals_used = self._used_globals(code, frame.f_globals)
        global_refs, global_errors = self._capture_mapping(globals_used, ignored)

        call_attributes = dict(attributes or {})
        if instruction_offset is not None:
            call_attributes.setdefault("entry_instruction_offset", instruction_offset)
        capture_errors = {**local_errors, **global_errors}
        if capture_errors:
            call_attributes["capture_errors"] = capture_errors

        captured_caller = self._nearest_active_call(frame.f_back)

        function_call = FunctionCall(
            function_name=code.co_name,
            qualified_name=code.co_qualname,
            module_name=frame.f_globals.get("__name__"),
            file_path=code.co_filename,
            first_line_number=code.co_firstlineno,
            started_at=self._now(),
            outcome=FunctionCallOutcome.RUNNING,
            entry_locals_refs=local_refs,
            entry_globals_refs=global_refs,
            attributes=call_attributes,
            code_definition_id=self._store_code_definition(code),
            caller_call=(
                captured_caller.function_call if captured_caller is not None else None
            ),
        )
        self.database.add(function_call)

        active = _ActiveCall(
            frame_id=frame_id,
            code=code,
            function_call=function_call,
            role=role,
        )
        self._active_calls.append(active)
        self._active_calls_by_frame[frame_id] = active

        if role == CallRole.STEP and execution_session.step_kind == StepKind.FUNCTION_CALL:
            active.current_step = self._create_step(
                kind=StepKind.FUNCTION_CALL,
                function_call=function_call,
                source_step=source_step,
            )
        elif role == CallRole.EXTERNAL_INTERACTION:
            occurrence = ExternalInteractionOccurrence(
                step=owner_step,
                function_call=function_call,
                position=self._take_external_position(owner_step),
            )
            self.database.add(occurrence)

        self._mark_event_recorded()
        return function_call

    def record_stack_snapshot(
        self,
        frame: types.FrameType,
        line_number: int,
        *,
        code: types.CodeType | None = None,
        instruction_offset: int | None = None,
        ignored_names: Collection[str] = (),
        attributes: Mapping[str, Any] | None = None,
        source_step: ExecutionStep | None = None,
    ) -> StackSnapshot | None:
        """Record a selected ``LINE`` event for an active captured frame."""

        if not self.is_recording_enabled:
            return None
        execution_session = self._require_session()
        self._require_branch()

        code = code or frame.f_code
        active = self._active_calls_by_frame.get(id(frame))
        if active is None:
            raise MonitoringStateError(
                f"Cannot record a snapshot for inactive frame {code.co_qualname}"
            )

        ignored = frozenset(ignored_names)
        local_refs, local_errors = self._capture_mapping(frame.f_locals, ignored)
        globals_used = self._used_globals(code, frame.f_globals)
        global_refs, global_errors = self._capture_mapping(globals_used, ignored)

        snapshot_attributes = dict(attributes or {})
        capture_errors = {**local_errors, **global_errors}
        if capture_errors:
            snapshot_attributes["capture_errors"] = capture_errors

        snapshot = StackSnapshot(
            function_call=active.function_call,
            code_definition_id=self._store_code_definition(code),
            line_number=line_number,
            instruction_offset=instruction_offset,
            captured_at=self._now(),
            locals_refs=local_refs,
            globals_refs=global_refs,
            attributes=snapshot_attributes,
        )
        self.database.add(snapshot)

        if (
            active.role == CallRole.STEP
            and execution_session.step_kind == StepKind.STACK_SNAPSHOT
        ):
            active.current_step = self._create_step(
                kind=StepKind.STACK_SNAPSHOT,
                stack_snapshot=snapshot,
                source_step=source_step,
            )

        self._mark_event_recorded()
        return snapshot

    def record_function_return(
        self,
        frame: types.FrameType,
        return_value: Any,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> FunctionCall | None:
        """Record a selected ``PY_RETURN`` event and close its VM call."""

        active = self._active_calls_by_frame.get(id(frame))
        if active is None:
            return None
        self._ensure_top_active(active)

        call = active.function_call
        return_ref = None
        return_error = None
        try:
            return_ref = self._store_value(return_value)
        except Exception as error:  # noqa: BLE001 - capture must not hide the call
            return_error = f"{type(error).__name__}: {error}"

        call.return_ref = return_ref
        call.exception_ref = None
        call.outcome = FunctionCallOutcome.RETURNED
        call.completed_at = self._now()
        self._merge_call_attributes(call, attributes, "return", return_error)

        self._remove_active(active)
        self._mark_event_recorded()
        return call

    def record_function_unwind(
        self,
        frame: types.FrameType,
        exception: BaseException,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> FunctionCall | None:
        """Record a selected ``PY_UNWIND`` event and its raised exception."""

        active = self._active_calls_by_frame.get(id(frame))
        if active is None:
            return None
        self._ensure_top_active(active)

        call = active.function_call
        exception_ref = None
        exception_error = None
        try:
            exception_ref = self._store_value(exception)
        except Exception as error:  # noqa: BLE001 - capture must not hide the call
            exception_error = f"{type(error).__name__}: {error}"

        call.return_ref = None
        call.exception_ref = exception_ref
        call.outcome = FunctionCallOutcome.RAISED
        call.completed_at = self._now()
        self._merge_call_attributes(call, attributes, "exception", exception_error)

        self._remove_active(active)
        self._mark_event_recorded()
        return call

    def _create_step(
        self,
        *,
        kind: StepKind,
        function_call: FunctionCall | None = None,
        stack_snapshot: StackSnapshot | None = None,
        source_step: ExecutionStep | None = None,
    ) -> ExecutionStep:
        branch = self._require_branch()
        position = self._next_step_position

        if position == 0 and branch.parent_branch is not None:
            if source_step is None:
                source_step = branch.forked_from_step
            if source_step is not branch.forked_from_step:
                raise MonitoringStateError(
                    "The first child-branch step must source its forked-from step"
                )

        step = ExecutionStep(
            branch=branch,
            position=position,
            kind=kind,
            function_call=function_call,
            stack_snapshot=stack_snapshot,
            source_step=source_step,
        )
        self.database.add(step)
        self._next_step_position += 1
        return step

    def _take_external_position(self, step: ExecutionStep) -> int:
        step_key = id(step)
        if step_key not in self._next_external_positions:
            last_position = max(
                (occurrence.position for occurrence in step.external_interactions),
                default=-1,
            )
            self._next_external_positions[step_key] = last_position + 1

        position = self._next_external_positions[step_key]
        self._next_external_positions[step_key] = position + 1
        return position

    def _nearest_active_call(
        self,
        frame: types.FrameType | None,
    ) -> _ActiveCall | None:
        """Find the closest captured caller through unmonitored frames."""

        while frame is not None:
            active = self._active_calls_by_frame.get(id(frame))
            if active is not None:
                return active
            frame = frame.f_back
        return None

    def _nearest_owner_step(
        self,
        frame: types.FrameType | None,
    ) -> ExecutionStep | None:
        """Find the closest active timeline step in the actual frame stack."""

        while frame is not None:
            active = self._active_calls_by_frame.get(id(frame))
            if active is not None and active.current_step is not None:
                return active.current_step
            frame = frame.f_back
        return None

    def _ensure_top_active(self, active: _ActiveCall) -> None:
        if not self._active_calls or self._active_calls[-1] is not active:
            raise MonitoringStateError(
                f"Out-of-order return/unwind for {active.code.co_qualname}"
            )

    def _remove_active(self, active: _ActiveCall) -> None:
        self._active_calls.pop()
        self._active_calls_by_frame.pop(active.frame_id, None)

    def _merge_call_attributes(
        self,
        call: FunctionCall,
        attributes: Mapping[str, Any] | None,
        error_kind: str,
        capture_error: str | None,
    ) -> None:
        merged = dict(call.attributes)
        if attributes:
            merged.update(attributes)
        if capture_error:
            errors = dict(merged.get("capture_errors", {}))
            errors[error_kind] = capture_error
            merged["capture_errors"] = errors
        call.attributes = merged

    def _mark_event_recorded(self) -> None:
        self._pending_events += 1
        if (
            self.flush_batch_size > 0
            and self._pending_events >= self.flush_batch_size
        ):
            self.database.flush()
            self._pending_events = 0

    def _call_start_attributes(
        self,
        registration: CaptureRegistration,
        frame: types.FrameType,
        instruction_offset: int,
    ) -> Mapping[str, Any] | None:
        provider = registration.start_attributes
        if provider is None:
            return None
        return self._safe_attributes(
            "start_attributes",
            lambda: provider(self, frame, registration.code, instruction_offset),
        )

    def _call_return_attributes(
        self,
        registration: CaptureRegistration | None,
        frame: types.FrameType,
        code: types.CodeType,
        instruction_offset: int,
        return_value: Any,
    ) -> Mapping[str, Any] | None:
        if registration is None or registration.return_attributes is None:
            return None
        provider = registration.return_attributes
        return self._safe_attributes(
            "return_attributes",
            lambda: provider(
                self,
                frame,
                code,
                instruction_offset,
                return_value,
            ),
        )

    def _call_line_attributes(
        self,
        registration: CaptureRegistration,
        frame: types.FrameType,
        line_number: int,
    ) -> Mapping[str, Any] | None:
        provider = registration.line_attributes
        if provider is None:
            return None
        return self._safe_attributes(
            "line_attributes",
            lambda: provider(self, frame, registration.code, line_number),
        )

    def _safe_attributes(
        self,
        provider_name: str,
        invoke: Callable[[], Mapping[str, Any] | None],
    ) -> Mapping[str, Any] | None:
        try:
            attributes = invoke()
        except BaseException as error:  # interface hook must not alter execution
            return {
                "capture_errors": {
                    provider_name: f"{type(error).__name__}: {error}"
                }
            }
        if attributes is None:
            return None
        if not isinstance(attributes, Mapping):
            return {
                "capture_errors": {
                    provider_name: "attribute provider did not return a mapping"
                }
            }
        return attributes

    def _handle_callback_error(self, error: BaseException) -> None:
        self.last_callback_error = error
        self.is_recording_enabled = False
        logger.exception("SpaceTimePy monitoring callback failed")
        if not self.database.is_active:
            self.database.rollback()
            self._pending_events = 0
            self._active_calls.clear()
            self._active_calls_by_frame.clear()

    @staticmethod
    def _code_from_target(
        target: types.CodeType | Callable[..., Any],
    ) -> types.CodeType:
        if isinstance(target, types.CodeType):
            return target
        code = getattr(target, "__code__", None)
        if code is None and hasattr(target, "__func__"):
            code = getattr(target.__func__, "__code__", None)
        if not isinstance(code, types.CodeType):
            raise TypeError("Capture target must be a Python callable or code object")
        return code

    def _capture_mapping(
        self,
        values: Mapping[str, Any],
        ignored_names: Collection[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        references: dict[str, str] = {}
        errors: dict[str, str] = {}

        for name, value in values.items():
            if (
                name in ignored_names
                or name.startswith("__")
                or callable(value)
                or isinstance(value, types.ModuleType)
            ):
                continue
            try:
                references[name] = self._store_value(value)
            except Exception as error:  # noqa: BLE001 - record other variables
                errors[name] = f"{type(error).__name__}: {error}"

        return references, errors

    def _store_value(self, value: Any) -> str:
        type_name = f"{type(value).__module__}.{type(value).__qualname__}"
        is_primitive = isinstance(value, int | float | bool | str | type(None))

        if is_primitive:
            canonical = json.dumps(
                {"type": type_name, "value": value},
                allow_nan=True,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            state_digest = hashlib.sha256(canonical).hexdigest()
            identity_hash = f"primitive:{state_digest}"
            object_reference = state_digest
            pickle_data = None
            primitive_value = value
        else:
            pickle_data = self.serializer.dumps(value)
            state_digest = hashlib.sha256(pickle_data).hexdigest()
            identity_hash = f"runtime:{self._identity_namespace}:{id(value)}"
            object_reference = hashlib.sha256(
                f"{identity_hash}:{state_digest}".encode()
            ).hexdigest()
            primitive_value = None

        cache_key = (identity_hash, state_digest)
        cached_reference = self._stored_value_cache.get(cache_key)
        if cached_reference is not None:
            return cached_reference

        # Runtime identities include this monitor's namespace, so only stable
        # primitive references can already exist from an earlier recording.
        if is_primitive:
            with self.database.no_autoflush:
                existing = self.database.get(StoredObject, object_reference)
            if existing is not None:
                self._stored_value_cache[cache_key] = existing.id
                return existing.id

        identity = self._identity_cache.get(identity_hash)
        if identity is None:
            identity = ObjectIdentity(identity_hash=identity_hash, name=type_name)
            self.database.add(identity)
        self._identity_cache[identity_hash] = identity

        version_number = self._next_object_versions.get(identity_hash, 1)
        self._next_object_versions[identity_hash] = version_number + 1
        stored = StoredObject(
            id=object_reference,
            identity=identity,
            version_number=version_number,
            type_name=type_name,
            is_primitive=is_primitive,
            primitive_value=primitive_value,
            pickle_data=pickle_data,
        )
        self.database.add(stored)
        self._stored_value_cache[cache_key] = object_reference
        return object_reference

    def _store_code_definition(self, code: types.CodeType) -> str | None:
        if code in self._code_definition_cache:
            return self._code_definition_cache[code]

        try:
            code_content = inspect.getsource(code)
            first_line_number = inspect.getsourcelines(code)[1]
        except (OSError, TypeError):
            self._code_definition_cache[code] = None
            return None

        definition_id = hashlib.sha256(code_content.encode()).hexdigest()
        if definition_id not in self._known_code_definition_ids:
            with self.database.no_autoflush:
                definition = self.database.get(CodeDefinition, definition_id)
            if definition is None:
                definition = CodeDefinition(
                    id=definition_id,
                    name=code.co_name,
                    qualified_name=code.co_qualname,
                    kind="function",
                    module_path=code.co_filename,
                    code_content=code_content,
                    first_line_number=first_line_number,
                )
                self.database.add(definition)
            self._known_code_definition_ids.add(definition_id)

        self._code_definition_cache[code] = definition_id
        return definition_id

    def _used_globals(
        self,
        code: types.CodeType,
        global_namespace: Mapping[str, Any],
    ) -> dict[str, Any]:
        names = self._global_names_for_code(code)

        return {
            name: global_namespace[name]
            for name in names
            if name in global_namespace
        }

    def _global_names_for_code(self, code: types.CodeType) -> frozenset[str]:
        names = self._global_names_cache.get(code)
        if names is None:
            names = frozenset(
                instruction.argval
                for instruction in dis.get_instructions(code)
                if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}
                and isinstance(instruction.argval, str)
            )
            self._global_names_cache[code] = names
        return names

    def _require_session(self) -> ExecutionSession:
        if self.current_session is None:
            raise MonitoringStateError("No execution session is currently being recorded")
        return self.current_session

    def _require_branch(self) -> ExecutionBranch:
        if self.current_branch is None:
            raise MonitoringStateError("No execution branch is currently being recorded")
        return self.current_branch

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


__all__ = [
    "CallRole",
    "CaptureRegistration",
    "MonitoringStateError",
    "SpaceTimeMonitor",
]
