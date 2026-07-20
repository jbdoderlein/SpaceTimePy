"""Public capture declaration and recording lifecycle interface."""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import TYPE_CHECKING, Any, TypeVar, overload

from spacetimepy.core.model import (
    ExecutionBranch,
    ExecutionSession,
    ExecutionStatus,
    StepKind,
    utc_now,
)
from spacetimepy.core.monitoring import CallRole

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator

    from sqlalchemy.orm import Session

    from spacetimepy.core.monitoring import (
        LineAttributes,
        ReturnAttributes,
        SpaceTimeMonitor,
        StartAttributes,
    )
    from spacetimepy.interface.standard_external_interactions import (
        StandardExternalInteractionRegistry,
    )


CapturedCallable = TypeVar("CapturedCallable", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class CaptureStartContext:
    """Read-only VM context supplied to a capture start hook.

    The mappings contain the values visible when the captured function starts.
    They are inputs to the hook only: the hook must return JSON-compatible
    metadata for anything that should be persisted in the trace.
    """

    function_name: str
    qualified_name: str
    module_name: str | None
    file_path: str
    first_line_number: int
    instruction_offset: int
    locals: Mapping[str, Any]
    globals: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CaptureReturnContext(CaptureStartContext):
    """Read-only VM context supplied to a capture return hook."""

    return_value: Any


type StartHook = Callable[
    [CaptureStartContext],
    Mapping[str, Any] | None,
]
type ReturnHook = Callable[
    [CaptureReturnContext],
    Mapping[str, Any] | None,
]


def _context_values(
    frame: types.FrameType,
    code: types.CodeType,
    instruction_offset: int,
) -> dict[str, Any]:
    module_name = frame.f_globals.get("__name__")
    return {
        "function_name": code.co_name,
        "qualified_name": code.co_qualname,
        "module_name": module_name if isinstance(module_name, str) else None,
        "file_path": code.co_filename,
        "first_line_number": code.co_firstlineno,
        "instruction_offset": instruction_offset,
        # f_locals is copied because its contents may be refreshed by the VM.
        # Globals remain live to avoid copying an entire module for every hook.
        "locals": types.MappingProxyType(dict(frame.f_locals)),
        "globals": types.MappingProxyType(frame.f_globals),
    }


def _hook_name(hook: Callable[..., Any]) -> str:
    return getattr(hook, "__qualname__", getattr(hook, "__name__", repr(hook)))


def _run_hooks(
    hooks: tuple[StartHook, ...] | tuple[ReturnHook, ...],
    context: CaptureStartContext | CaptureReturnContext,
    *,
    phase: str,
) -> dict[str, Any]:
    """Run independent hooks and merge their JSON metadata in list order."""

    metadata: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for position, hook in enumerate(hooks):
        error_key = f"{phase}[{position}] {_hook_name(hook)}"
        try:
            result = hook(context)  # type: ignore[arg-type]
            if result is None:
                continue
            if not isinstance(result, Mapping):
                raise TypeError("hook did not return a mapping")
            hook_metadata = dict(result)
            if not all(isinstance(key, str) for key in hook_metadata):
                raise TypeError("hook metadata keys must be strings")
            # Trace attributes use a SQL JSON column.  Reject unsupported data
            # at the hook boundary instead of failing a later batch flush.
            json.dumps(hook_metadata)
        except BaseException as error:  # hooks must not alter user execution
            errors[error_key] = f"{type(error).__name__}: {error}"
            continue
        metadata.update(hook_metadata)

    if errors:
        metadata[f"{phase}_hook_errors"] = errors
    return metadata


def _combine_start_attributes(
    provider: StartAttributes | None,
    hooks: tuple[StartHook, ...],
) -> StartAttributes | None:
    if not hooks:
        return provider

    def attributes(
        monitor: SpaceTimeMonitor,
        frame: types.FrameType,
        code: types.CodeType,
        instruction_offset: int,
    ) -> Mapping[str, Any] | None:
        merged = dict(
            provider(monitor, frame, code, instruction_offset) or {}
            if provider is not None
            else {}
        )
        context = CaptureStartContext(
            **_context_values(frame, code, instruction_offset)
        )
        merged.update(_run_hooks(hooks, context, phase="start"))
        return merged or None

    return attributes


def _combine_return_attributes(
    provider: ReturnAttributes | None,
    hooks: tuple[ReturnHook, ...],
) -> ReturnAttributes | None:
    if not hooks:
        return provider

    def attributes(
        monitor: SpaceTimeMonitor,
        frame: types.FrameType,
        code: types.CodeType,
        instruction_offset: int,
        return_value: Any,
    ) -> Mapping[str, Any] | None:
        merged = dict(
            provider(monitor, frame, code, instruction_offset, return_value) or {}
            if provider is not None
            else {}
        )
        context = CaptureReturnContext(
            **_context_values(frame, code, instruction_offset),
            return_value=return_value,
        )
        merged.update(_run_hooks(hooks, context, phase="return"))
        return merged or None

    return attributes


class CaptureMode(StrEnum):
    """Granularity used for the timeline of one execution session."""

    FUNCTION = "function_call"
    LINE = "stack_snapshot"


@dataclass(frozen=True, slots=True)
class RecordingHandle:
    """Stable public identity of a recording while it is active."""

    session_id: int
    branch_id: int
    mode: CaptureMode


@dataclass(frozen=True, slots=True)
class CaptureDeclaration:
    """Process-level capture configuration independent of a runtime."""

    target: Callable[..., Any]
    role: CallRole
    capture_lines: bool = False
    line_numbers: frozenset[int] | None = None
    ignored_names: frozenset[str] = frozenset()
    start_attributes: StartAttributes | None = None
    return_attributes: ReturnAttributes | None = None
    line_attributes: LineAttributes | None = None
    start_hooks: tuple[StartHook, ...] = ()
    return_hooks: tuple[ReturnHook, ...] = ()

    @property
    def code(self) -> types.CodeType:
        code = getattr(self.target, "__code__", None)
        if code is None and hasattr(self.target, "__func__"):
            code = getattr(self.target.__func__, "__code__", None)
        if not isinstance(code, types.CodeType):
            raise TypeError("Capture target must be a Python callable")
        return code

    def install(self, monitor: SpaceTimeMonitor) -> None:
        monitor.register_capture(
            self.target,
            role=self.role,
            capture_lines=self.capture_lines,
            line_numbers=self.line_numbers,
            ignored_names=self.ignored_names,
            start_attributes=_combine_start_attributes(
                self.start_attributes,
                self.start_hooks,
            ),
            return_attributes=_combine_return_attributes(
                self.return_attributes,
                self.return_hooks,
            ),
            line_attributes=self.line_attributes,
        )


class CaptureRegistry:
    """Store import-time declarations and bind them to the active monitor."""

    def __init__(self) -> None:
        self._declarations: dict[types.CodeType, CaptureDeclaration] = {}
        self._monitor: SpaceTimeMonitor | None = None
        self._lock = RLock()

    @property
    def declarations(self) -> tuple[CaptureDeclaration, ...]:
        with self._lock:
            return tuple(self._declarations.values())

    def declare(self, declaration: CaptureDeclaration) -> None:
        with self._lock:
            code = declaration.code
            previous = self._declarations.get(code)
            self._declarations[code] = declaration
            if self._monitor is None:
                return
            try:
                declaration.install(self._monitor)
            except BaseException:
                if previous is None:
                    self._declarations.pop(code, None)
                    self._monitor.unregister_capture(declaration.target)
                else:
                    self._declarations[code] = previous
                    previous.install(self._monitor)
                raise

    def unregister(self, target: Callable[..., Any]) -> bool:
        declaration = CaptureDeclaration(target=target, role=CallRole.STEP)
        with self._lock:
            removed = self._declarations.pop(declaration.code, None)
            if removed is not None and self._monitor is not None:
                self._monitor.unregister_capture(target)
            return removed is not None

    def clear(self) -> None:
        with self._lock:
            declarations = tuple(self._declarations.values())
            self._declarations.clear()
            if self._monitor is not None:
                for declaration in declarations:
                    self._monitor.unregister_capture(declaration.target)

    def bind(self, monitor: SpaceTimeMonitor) -> None:
        with self._lock:
            if self._monitor is monitor:
                return
            if self._monitor is not None:
                raise RuntimeError("Capture declarations are bound to another runtime")
            for declaration in self._declarations.values():
                declaration.install(monitor)
            self._monitor = monitor

    def unbind(self, monitor: SpaceTimeMonitor) -> None:
        with self._lock:
            if self._monitor is monitor:
                self._monitor = None


capture_registry = CaptureRegistry()


def _declare_capture[Target: Callable[..., Any]](
    target: Target | None,
    *,
    role: CallRole,
    capture_lines: bool = False,
    lines: Collection[int] | None = None,
    ignored_names: Collection[str] = (),
    start_attributes: StartAttributes | None = None,
    return_attributes: ReturnAttributes | None = None,
    line_attributes: LineAttributes | None = None,
    start_hooks: Iterable[StartHook] = (),
    return_hooks: Iterable[ReturnHook] = (),
) -> Target | Callable[[Target], Target]:
    selected_start_hooks = tuple(start_hooks)
    selected_return_hooks = tuple(return_hooks)

    def decorate(function: Target) -> Target:
        capture_registry.declare(
            CaptureDeclaration(
                target=function,
                role=role,
                capture_lines=capture_lines,
                line_numbers=None if lines is None else frozenset(lines),
                ignored_names=frozenset(ignored_names),
                start_attributes=start_attributes,
                return_attributes=return_attributes,
                line_attributes=line_attributes,
                start_hooks=selected_start_hooks,
                return_hooks=selected_return_hooks,
            )
        )
        return function

    return decorate(target) if target is not None else decorate


@overload
def function[Target: Callable[..., Any]](
    target: Target, /, **options: Any
) -> Target: ...


@overload
def function[Target: Callable[..., Any]](
    target: None = None, /, **options: Any
) -> Callable[[Target], Target]: ...


def function[Target: Callable[..., Any]](
    target: Target | None = None,
    /,
    *,
    ignored_names: Collection[str] = (),
    start_attributes: StartAttributes | None = None,
    return_attributes: ReturnAttributes | None = None,
    start_hooks: Iterable[StartHook] = (),
    return_hooks: Iterable[ReturnHook] = (),
) -> Target | Callable[[Target], Target]:
    """Declare import-time function-call capture for the next/current runtime."""

    return _declare_capture(
        target,
        role=CallRole.STEP,
        ignored_names=ignored_names,
        start_attributes=start_attributes,
        return_attributes=return_attributes,
        start_hooks=start_hooks,
        return_hooks=return_hooks,
    )


@overload
def line[Target: Callable[..., Any]](
    target: Target, /, **options: Any
) -> Target: ...


@overload
def line[Target: Callable[..., Any]](
    target: None = None, /, **options: Any
) -> Callable[[Target], Target]: ...


def line[Target: Callable[..., Any]](
    target: Target | None = None,
    /,
    *,
    lines: Collection[int] | None = None,
    ignored_names: Collection[str] = (),
    start_attributes: StartAttributes | None = None,
    return_attributes: ReturnAttributes | None = None,
    line_attributes: LineAttributes | None = None,
    start_hooks: Iterable[StartHook] = (),
    return_hooks: Iterable[ReturnHook] = (),
) -> Target | Callable[[Target], Target]:
    """Declare import-time line capture for the next/current runtime."""

    return _declare_capture(
        target,
        role=CallRole.STEP,
        capture_lines=True,
        lines=lines,
        ignored_names=ignored_names,
        start_attributes=start_attributes,
        return_attributes=return_attributes,
        line_attributes=line_attributes,
        start_hooks=start_hooks,
        return_hooks=return_hooks,
    )


@overload
def support[Target: Callable[..., Any]](
    target: Target, /, **options: Any
) -> Target: ...


@overload
def support[Target: Callable[..., Any]](
    target: None = None, /, **options: Any
) -> Callable[[Target], Target]: ...


def support[Target: Callable[..., Any]](
    target: Target | None = None,
    /,
    *,
    ignored_names: Collection[str] = (),
    start_attributes: StartAttributes | None = None,
    return_attributes: ReturnAttributes | None = None,
    start_hooks: Iterable[StartHook] = (),
    return_hooks: Iterable[ReturnHook] = (),
) -> Target | Callable[[Target], Target]:
    """Declare an import-time supporting VM call capture."""

    return _declare_capture(
        target,
        role=CallRole.SUPPORT,
        ignored_names=ignored_names,
        start_attributes=start_attributes,
        return_attributes=return_attributes,
        start_hooks=start_hooks,
        return_hooks=return_hooks,
    )


@overload
def external[Target: Callable[..., Any]](
    target: Target, /, **options: Any
) -> Target: ...


@overload
def external[Target: Callable[..., Any]](
    target: None = None, /, **options: Any
) -> Callable[[Target], Target]: ...


def external[Target: Callable[..., Any]](
    target: Target | None = None,
    /,
    *,
    ignored_names: Collection[str] = (),
    start_attributes: StartAttributes | None = None,
    return_attributes: ReturnAttributes | None = None,
    start_hooks: Iterable[StartHook] = (),
    return_hooks: Iterable[ReturnHook] = (),
) -> Target | Callable[[Target], Target]:
    """Declare an import-time ordered external-interaction capture."""

    return _declare_capture(
        target,
        role=CallRole.EXTERNAL_INTERACTION,
        ignored_names=ignored_names,
        start_attributes=start_attributes,
        return_attributes=return_attributes,
        start_hooks=start_hooks,
        return_hooks=return_hooks,
    )


def unregister_capture_declaration(target: Callable[..., Any]) -> bool:
    """Remove one process-level declaration and its active registration."""

    return capture_registry.unregister(target)


def clear_capture_declarations() -> None:
    """Remove all process-level declarations and active registrations."""

    capture_registry.clear()


class CaptureInterface:
    """Declare captured code and control root-branch recording.

    Bound decorators install runtime-local configuration. Package-level
    decorators use :class:`CaptureRegistry` and are reinstalled for each
    runtime. Neither form is stored in the trace model.
    """

    def __init__(
        self,
        database: Session,
        monitor: SpaceTimeMonitor,
        standard_external_interactions: (
            StandardExternalInteractionRegistry | None
        ) = None,
    ) -> None:
        self._database = database
        self._monitor = monitor
        self._standard_external_interactions = standard_external_interactions

    @property
    def is_enabled(self) -> bool:
        """Whether registered VM events are currently being recorded."""

        return self._monitor.is_recording_enabled

    @property
    def last_error(self) -> BaseException | None:
        """Last error suppressed at the VM callback boundary, if any."""

        return self._monitor.last_callback_error

    def enable(self) -> None:
        self._monitor.enable_recording()

    def disable(self) -> None:
        self._monitor.disable_recording()

    @contextmanager
    def disabled(self) -> Iterator[None]:
        """Temporarily pause capture without changing registrations."""

        with self._monitor.recording_disabled():
            yield

    @overload
    def function(
        self, target: CapturedCallable, /, **options: Any
    ) -> CapturedCallable: ...

    @overload
    def function(
        self, target: None = None, /, **options: Any
    ) -> Callable[[CapturedCallable], CapturedCallable]: ...

    def function(
        self,
        target: CapturedCallable | None = None,
        /,
        *,
        ignored_names: Collection[str] = (),
        start_attributes: StartAttributes | None = None,
        return_attributes: ReturnAttributes | None = None,
        start_hooks: Iterable[StartHook] = (),
        return_hooks: Iterable[ReturnHook] = (),
    ) -> CapturedCallable | Callable[[CapturedCallable], CapturedCallable]:
        """Capture each invocation of the decorated function as a step."""

        combined_start_attributes = _combine_start_attributes(
            start_attributes,
            tuple(start_hooks),
        )
        combined_return_attributes = _combine_return_attributes(
            return_attributes,
            tuple(return_hooks),
        )

        def decorate(function: CapturedCallable) -> CapturedCallable:
            self._monitor.register_capture(
                function,
                role=CallRole.STEP,
                ignored_names=ignored_names,
                start_attributes=combined_start_attributes,
                return_attributes=combined_return_attributes,
            )
            return function

        return decorate(target) if target is not None else decorate

    @overload
    def line(
        self, target: CapturedCallable, /, **options: Any
    ) -> CapturedCallable: ...

    @overload
    def line(
        self, target: None = None, /, **options: Any
    ) -> Callable[[CapturedCallable], CapturedCallable]: ...

    def line(
        self,
        target: CapturedCallable | None = None,
        /,
        *,
        lines: Collection[int] | None = None,
        ignored_names: Collection[str] = (),
        start_attributes: StartAttributes | None = None,
        return_attributes: ReturnAttributes | None = None,
        line_attributes: LineAttributes | None = None,
        start_hooks: Iterable[StartHook] = (),
        return_hooks: Iterable[ReturnHook] = (),
    ) -> CapturedCallable | Callable[[CapturedCallable], CapturedCallable]:
        """Capture selected line states of the decorated function as steps."""

        combined_start_attributes = _combine_start_attributes(
            start_attributes,
            tuple(start_hooks),
        )
        combined_return_attributes = _combine_return_attributes(
            return_attributes,
            tuple(return_hooks),
        )

        def decorate(function: CapturedCallable) -> CapturedCallable:
            self._monitor.register_capture(
                function,
                role=CallRole.STEP,
                capture_lines=True,
                line_numbers=lines,
                ignored_names=ignored_names,
                start_attributes=combined_start_attributes,
                return_attributes=combined_return_attributes,
                line_attributes=line_attributes,
            )
            return function

        return decorate(target) if target is not None else decorate

    @overload
    def support(
        self, target: CapturedCallable, /, **options: Any
    ) -> CapturedCallable: ...

    @overload
    def support(
        self, target: None = None, /, **options: Any
    ) -> Callable[[CapturedCallable], CapturedCallable]: ...

    def support(
        self,
        target: CapturedCallable | None = None,
        /,
        *,
        ignored_names: Collection[str] = (),
        start_attributes: StartAttributes | None = None,
        return_attributes: ReturnAttributes | None = None,
        start_hooks: Iterable[StartHook] = (),
        return_hooks: Iterable[ReturnHook] = (),
    ) -> CapturedCallable | Callable[[CapturedCallable], CapturedCallable]:
        """Capture a nested VM call without making it a timeline step."""

        combined_start_attributes = _combine_start_attributes(
            start_attributes,
            tuple(start_hooks),
        )
        combined_return_attributes = _combine_return_attributes(
            return_attributes,
            tuple(return_hooks),
        )

        def decorate(function: CapturedCallable) -> CapturedCallable:
            self._monitor.register_capture(
                function,
                role=CallRole.SUPPORT,
                ignored_names=ignored_names,
                start_attributes=combined_start_attributes,
                return_attributes=combined_return_attributes,
            )
            return function

        return decorate(target) if target is not None else decorate

    @overload
    def external(
        self, target: CapturedCallable, /, **options: Any
    ) -> CapturedCallable: ...

    @overload
    def external(
        self, target: None = None, /, **options: Any
    ) -> Callable[[CapturedCallable], CapturedCallable]: ...

    def external(
        self,
        target: CapturedCallable | None = None,
        /,
        *,
        ignored_names: Collection[str] = (),
        start_attributes: StartAttributes | None = None,
        return_attributes: ReturnAttributes | None = None,
        start_hooks: Iterable[StartHook] = (),
        return_hooks: Iterable[ReturnHook] = (),
    ) -> CapturedCallable | Callable[[CapturedCallable], CapturedCallable]:
        """Capture a nested call as an ordered external interaction."""

        combined_start_attributes = _combine_start_attributes(
            start_attributes,
            tuple(start_hooks),
        )
        combined_return_attributes = _combine_return_attributes(
            return_attributes,
            tuple(return_hooks),
        )

        def decorate(function: CapturedCallable) -> CapturedCallable:
            self._monitor.register_capture(
                function,
                role=CallRole.EXTERNAL_INTERACTION,
                ignored_names=ignored_names,
                start_attributes=combined_start_attributes,
                return_attributes=combined_return_attributes,
            )
            return function

        return decorate(target) if target is not None else decorate

    def unregister(self, target: Callable[..., Any]) -> bool:
        """Remove one transient capture declaration."""

        removed_declaration = capture_registry.unregister(target)
        removed_registration = self._monitor.unregister_capture(target) is not None
        return removed_declaration or removed_registration

    def clear(self) -> None:
        """Remove user declarations while preserving standard interactions."""

        capture_registry.clear()
        self._monitor.clear_captures()
        if self._standard_external_interactions is not None:
            self._standard_external_interactions.refresh()

    def annotate_session(
        self,
        session_id: int,
        attributes: Mapping[str, Any],
        *,
        commit: bool = True,
    ) -> None:
        """Merge JSON metadata into an execution session."""

        execution_session = self._database.get(ExecutionSession, session_id)
        if execution_session is None:
            raise LookupError(f"No execution session with id {session_id!r}")
        selected = self._validated_attributes(attributes)
        execution_session.attributes = {
            **execution_session.attributes,
            **selected,
        }
        self._database.flush()
        if commit:
            self._database.commit()

    def annotate_branch(
        self,
        branch_id: int,
        attributes: Mapping[str, Any],
        *,
        commit: bool = True,
    ) -> None:
        """Merge JSON metadata into an execution branch."""

        execution_branch = self._database.get(ExecutionBranch, branch_id)
        if execution_branch is None:
            raise LookupError(f"No execution branch with id {branch_id!r}")
        selected = self._validated_attributes(attributes)
        execution_branch.attributes = {
            **execution_branch.attributes,
            **selected,
        }
        self._database.flush()
        if commit:
            self._database.commit()

    @staticmethod
    def _validated_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
        selected = dict(attributes)
        if not all(isinstance(key, str) for key in selected):
            raise TypeError("attribute keys must be strings")
        json.dumps(selected)
        return selected

    def begin_recording(
        self,
        *,
        mode: CaptureMode | str = CaptureMode.FUNCTION,
        name: str | None = None,
        description: str | None = None,
        branch_name: str | None = "main",
        attributes: Mapping[str, Any] | None = None,
        branch_attributes: Mapping[str, Any] | None = None,
    ) -> RecordingHandle:
        """Create a session/root branch and attach the VM recorder to it."""

        if self._monitor.current_branch is not None:
            raise RuntimeError("A SpaceTime recording is already active")

        selected_mode = self._capture_mode(mode)
        execution_session = ExecutionSession(
            name=name,
            description=description,
            step_kind=StepKind(selected_mode.value),
            status=ExecutionStatus.OPEN,
            attributes=dict(attributes or {}),
        )
        root_branch = ExecutionBranch(
            session=execution_session,
            name=branch_name,
            status=ExecutionStatus.OPEN,
            attributes=dict(branch_attributes or {}),
        )
        self._database.add(execution_session)
        self._monitor.start_branch(root_branch)

        # Starting a recording is a cold-path operation.  Flush once so the
        # public handle contains stable IDs; events remain batch-flushed.
        self._monitor.flush()
        if execution_session.id is None or root_branch.id is None:
            raise RuntimeError("The database did not assign recording IDs")
        return RecordingHandle(
            session_id=execution_session.id,
            branch_id=root_branch.id,
            mode=selected_mode,
        )

    def finish_recording(
        self,
        status: str = "completed",
        *,
        commit: bool = True,
    ) -> RecordingHandle:
        """Finish the currently active root recording."""

        branch = self._monitor.current_branch
        execution_session = self._monitor.current_session
        if branch is None or execution_session is None:
            raise RuntimeError("No SpaceTime recording is active")
        if branch.parent_branch is not None:
            raise RuntimeError("The active branch is a replay, not a root recording")

        selected_status = ExecutionStatus(status)
        mode = CaptureMode(execution_session.step_kind.value)
        self._monitor.finish_branch(selected_status, commit=False)
        execution_session.status = selected_status
        execution_session.completed_at = (
            None if selected_status == ExecutionStatus.OPEN else utc_now()
        )
        self._database.flush()
        if commit:
            self._database.commit()

        if execution_session.id is None or branch.id is None:
            raise RuntimeError("The database did not assign recording IDs")
        return RecordingHandle(
            session_id=execution_session.id,
            branch_id=branch.id,
            mode=mode,
        )

    @contextmanager
    def recording(
        self,
        *,
        mode: CaptureMode | str = CaptureMode.FUNCTION,
        name: str | None = None,
        description: str | None = None,
        branch_name: str | None = "main",
        attributes: Mapping[str, Any] | None = None,
        branch_attributes: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> Iterator[RecordingHandle]:
        """Record a root execution branch within a context manager."""

        handle = self.begin_recording(
            mode=mode,
            name=name,
            description=description,
            branch_name=branch_name,
            attributes=attributes,
            branch_attributes=branch_attributes,
        )
        try:
            yield handle
        except BaseException:
            self._finish_recording_scope(
                handle,
                ExecutionStatus.FAILED,
                commit=commit,
            )
            raise
        else:
            self._finish_recording_scope(
                handle,
                ExecutionStatus.COMPLETED,
                commit=commit,
            )

    def _finish_recording_scope(
        self,
        handle: RecordingHandle,
        status: ExecutionStatus,
        *,
        commit: bool,
    ) -> None:
        branch = self._monitor.current_branch
        execution_session = self._monitor.current_session
        if branch is not None and execution_session is not None:
            if execution_session.id != handle.session_id:
                raise RuntimeError("Another SpaceTime session replaced this recording")
            if branch.parent_branch is not None:
                raise RuntimeError(
                    "The active replay must be finished before its root recording exits"
                )
            self.finish_recording(status.value, commit=commit)
            return

        persisted_session = self._database.get(ExecutionSession, handle.session_id)
        if persisted_session is None or persisted_session.status == ExecutionStatus.OPEN:
            raise RuntimeError("The SpaceTime recording ended without an active branch")
        if commit:
            self._database.commit()

    @staticmethod
    def _capture_mode(mode: CaptureMode | str) -> CaptureMode:
        aliases = {
            "function": CaptureMode.FUNCTION,
            "line": CaptureMode.LINE,
        }
        if isinstance(mode, str) and mode in aliases:
            return aliases[mode]
        return CaptureMode(mode)


__all__ = [
    "CaptureDeclaration",
    "CaptureInterface",
    "CaptureMode",
    "CaptureRegistry",
    "CaptureReturnContext",
    "CaptureStartContext",
    "RecordingHandle",
    "ReturnHook",
    "StartHook",
    "clear_capture_declarations",
    "external",
    "function",
    "line",
    "support",
    "unregister_capture_declaration",
]
