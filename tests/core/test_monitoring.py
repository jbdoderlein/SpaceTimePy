from __future__ import annotations

import sys

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from spacetimepy.core.model import (
    ExecutionStep,
    FunctionCall,
    FunctionCallOutcome,
    StackSnapshot,
    StepKind,
)
from spacetimepy.core.monitoring import (
    CallRole,
    MonitoringStateError,
    SpaceTimeMonitor,
)
from spacetimepy.interface.data import TraceData
from tests.support import DatabaseTestCase

CALLED_FUNCTION_GLOBAL = {"source": "called function"}
RECURSIVE_FUNCTION_GLOBAL = {"source": "recursive function"}


def _read_called_function_global() -> str:
    return CALLED_FUNCTION_GLOBAL["source"]


def _call_global_reader() -> str:
    return _read_called_function_global()


def _recursive_global_a(depth: int) -> str:
    return _recursive_global_b(depth - 1)


def _recursive_global_b(depth: int) -> str:
    if depth <= 0:
        return RECURSIVE_FUNCTION_GLOBAL["source"]
    return _recursive_global_a(depth)


class TestSpaceTimeMonitor(DatabaseTestCase):
    def test_monitor_is_a_singleton_bound_to_one_orm_session(self) -> None:
        monitor = self.create_monitor()
        self.assertIs(SpaceTimeMonitor(self.database), monitor)

        other_engine = create_engine("sqlite+pysqlite:///:memory:")
        other_database = Session(other_engine)
        try:
            with self.assertRaises(MonitoringStateError):
                SpaceTimeMonitor(other_database)
        finally:
            other_database.close()
            other_engine.dispose()

    def test_registration_lifecycle_controls_local_vm_events(self) -> None:
        monitor = self.create_monitor()

        def calculate(value: int) -> int:
            return value + 1

        registration = monitor.register_capture(calculate, role=CallRole.STEP)

        self.assertEqual(registration.role, CallRole.STEP)
        self.assertIn(registration, monitor.captures)
        self.assertNotEqual(
            sys.monitoring.get_local_events(monitor.tool_id, calculate.__code__),
            0,
        )
        self.assertIs(monitor.unregister_capture(calculate), registration)
        self.assertEqual(
            sys.monitoring.get_local_events(monitor.tool_id, calculate.__code__),
            0,
        )

    def test_external_interaction_cannot_capture_lines(self) -> None:
        monitor = self.create_monitor()

        def external() -> int:
            return 1

        with self.assertRaises(ValueError):
            monitor.register_capture(
                external,
                role=CallRole.EXTERNAL_INTERACTION,
                capture_lines=True,
            )

    def test_line_limit_configuration_is_validated(self) -> None:
        monitor = self.create_monitor()

        def calculate() -> None:
            return None

        with self.assertRaisesRegex(TypeError, "must be an integer or None"):
            monitor.register_capture(
                calculate,
                capture_lines=True,
                max_snapshots_per_line=True,
            )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            monitor.register_capture(
                calculate,
                capture_lines=True,
                max_snapshots_per_line=0,
            )
        with self.assertRaisesRegex(ValueError, "requires line capture"):
            monitor.register_capture(calculate, max_snapshots_per_line=1)

    def test_function_start_and_return_create_vm_call_and_step(self) -> None:
        monitor = self.create_monitor()
        branch = self.start_branch(monitor)

        def calculate(value: int) -> int:
            return value + 1

        monitor.register_capture(calculate, role=CallRole.STEP)
        self.assertEqual(calculate(4), 5)
        monitor.finish_branch()

        call = self.database.scalars(select(FunctionCall)).one()
        step = self.database.scalars(select(ExecutionStep)).one()
        values = TraceData(self.database)
        self.assertEqual(call.outcome, FunctionCallOutcome.RETURNED)
        self.assertEqual(values.load_value(call.return_ref), 5)
        self.assertEqual(values.load_references(call.entry_locals_refs), {"value": 4})
        self.assertIs(step.function_call, call)
        self.assertIs(step.branch, branch)

    def test_entry_globals_include_globals_used_by_called_functions(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor)

        monitor.register_capture(_call_global_reader, role=CallRole.STEP)
        self.assertEqual(_call_global_reader(), "called function")
        monitor.finish_branch()

        call = self.database.scalars(select(FunctionCall)).one()
        globals_used = TraceData(self.database).load_references(call.entry_globals_refs)
        self.assertEqual(
            globals_used,
            {"CALLED_FUNCTION_GLOBAL": CALLED_FUNCTION_GLOBAL},
        )

    def test_line_globals_include_globals_used_by_called_functions(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor, kind=StepKind.STACK_SNAPSHOT)
        selected_line = _call_global_reader.__code__.co_firstlineno + 1

        monitor.register_capture(
            _call_global_reader,
            role=CallRole.STEP,
            capture_lines=True,
            line_numbers={selected_line},
        )
        self.assertEqual(_call_global_reader(), "called function")
        monitor.finish_branch()

        snapshot = self.database.scalars(select(StackSnapshot)).one()
        globals_used = TraceData(self.database).load_references(snapshot.globals_refs)
        self.assertEqual(
            globals_used,
            {"CALLED_FUNCTION_GLOBAL": CALLED_FUNCTION_GLOBAL},
        )

    def test_global_analysis_stops_at_recursive_function_cycles(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor)

        monitor.register_capture(_recursive_global_a, role=CallRole.STEP)
        self.assertEqual(_recursive_global_a(1), "recursive function")
        monitor.finish_branch()

        call = self.database.scalars(select(FunctionCall)).one()
        globals_used = TraceData(self.database).load_references(call.entry_globals_refs)
        self.assertEqual(
            globals_used,
            {"RECURSIVE_FUNCTION_GLOBAL": RECURSIVE_FUNCTION_GLOBAL},
        )

    def test_raised_exception_is_recorded_without_being_swallowed(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor)

        def explode() -> None:
            raise ValueError("boom")

        monitor.register_capture(explode, role=CallRole.STEP)
        with self.assertRaisesRegex(ValueError, "boom"):
            explode()
        monitor.finish_branch()

        call = self.database.scalars(select(FunctionCall)).one()
        exception = TraceData(self.database).load_value(call.exception_ref)
        self.assertEqual(call.outcome, FunctionCallOutcome.RAISED)
        self.assertIsInstance(exception, ValueError)
        self.assertEqual(str(exception), "boom")

    def test_support_call_keeps_vm_caller_without_creating_another_step(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor)

        def helper(value: int) -> int:
            return value * 2

        def outer(value: int) -> int:
            return helper(value) + 1

        monitor.register_capture(helper, role=CallRole.SUPPORT)
        monitor.register_capture(outer, role=CallRole.STEP)
        self.assertEqual(outer(3), 7)
        monitor.finish_branch()

        calls = self.database.scalars(
            select(FunctionCall).order_by(FunctionCall.started_at)
        ).all()
        steps = self.database.scalars(select(ExecutionStep)).all()
        outer_call = next(call for call in calls if call.function_name == "outer")
        helper_call = next(call for call in calls if call.function_name == "helper")
        self.assertEqual(len(steps), 1)
        self.assertIs(steps[0].function_call, outer_call)
        self.assertIs(helper_call.caller_call, outer_call)

    def test_external_interactions_are_attached_in_execution_order(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor)

        def read(value: int) -> int:
            return value

        def write(value: int) -> int:
            return value

        def tick() -> int:
            return write(read(1) + read(2))

        monitor.register_capture(read, role=CallRole.EXTERNAL_INTERACTION)
        monitor.register_capture(write, role=CallRole.EXTERNAL_INTERACTION)
        monitor.register_capture(tick, role=CallRole.STEP)
        self.assertEqual(tick(), 3)
        monitor.finish_branch()

        step = self.database.scalars(select(ExecutionStep)).one()
        self.assertEqual(
            [item.function_call.function_name for item in step.external_interactions],
            ["read", "read", "write"],
        )
        self.assertEqual(
            [item.position for item in step.external_interactions],
            [0, 1, 2],
        )

    def test_selected_line_event_creates_one_snapshot_step(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor, kind=StepKind.STACK_SNAPSHOT)

        def calculate(value: int) -> int:
            value += 1
            value *= 2
            return value

        selected_line = calculate.__code__.co_firstlineno + 2
        monitor.register_capture(
            calculate,
            role=CallRole.STEP,
            capture_lines=True,
            line_numbers={selected_line},
        )
        self.assertEqual(calculate(2), 6)
        monitor.finish_branch()

        steps = self.database.scalars(select(ExecutionStep)).all()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].kind, StepKind.STACK_SNAPSHOT)
        self.assertEqual(steps[0].stack_snapshot.line_number, selected_line)

    def test_line_limit_stores_first_snapshots_and_one_summary_per_call(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor, kind=StepKind.STACK_SNAPSHOT)
        hook_lines: list[int] = []

        def calculate(iterations: int) -> int:
            total = 0
            for index in range(iterations):
                total += index
                total *= 1
            return total

        def line_attributes(*args: object) -> dict[str, object]:
            line_number = args[-1]
            assert isinstance(line_number, int)
            hook_lines.append(line_number)
            return {"hook": True}

        selected_lines = {
            calculate.__code__.co_firstlineno + 3,
            calculate.__code__.co_firstlineno + 4,
        }
        monitor.register_capture(
            calculate,
            role=CallRole.STEP,
            capture_lines=True,
            line_numbers=selected_lines,
            max_snapshots_per_line=2,
            line_attributes=line_attributes,
        )

        self.assertEqual(calculate(5), 10)
        self.assertEqual(calculate(1), 0)
        monitor.finish_branch()

        calls = self.database.scalars(
            select(FunctionCall).order_by(FunctionCall.id)
        ).all()
        steps = self.database.scalars(
            select(ExecutionStep).order_by(ExecutionStep.position)
        ).all()
        self.assertEqual(len(steps), 6)
        for selected_line in selected_lines:
            self.assertEqual(hook_lines.count(selected_line), 3)
        self.assertEqual(
            calls[0].attributes["line_capture_limit"],
            {
                "maximum_per_line": 2,
                "lines": [
                    {
                        "code_definition_id": calls[0].code_definition_id,
                        "line_number": line_number,
                        "captured": 2,
                        "ignored": 3,
                    }
                    for line_number in sorted(selected_lines)
                ],
            },
        )
        self.assertNotIn("line_capture_limit", calls[1].attributes)

    def test_attribute_provider_errors_are_trace_data_not_program_errors(self) -> None:
        monitor = self.create_monitor()
        self.start_branch(monitor)

        def calculate() -> int:
            return 4

        def broken_provider(*args: object) -> dict[str, object]:
            del args
            raise RuntimeError("attribute failure")

        monitor.register_capture(
            calculate,
            role=CallRole.STEP,
            start_attributes=broken_provider,
        )
        self.assertEqual(calculate(), 4)
        monitor.finish_branch()

        call = self.database.scalars(select(FunctionCall)).one()
        self.assertIn("start_attributes", call.attributes["capture_errors"])
        self.assertTrue(monitor.is_recording_enabled)

    def test_event_flushes_are_batched_on_the_hot_path(self) -> None:
        monitor = self.create_monitor(flush_batch_size=2)
        self.start_branch(monitor)

        def calculate(value: int) -> int:
            return value + 1

        monitor.register_capture(calculate, role=CallRole.STEP)
        flushes = 0

        def count_flush(*args: object) -> None:
            nonlocal flushes
            del args
            flushes += 1

        event.listen(self.database, "after_flush", count_flush)
        try:
            self.assertEqual(calculate(1), 2)
            self.assertEqual(flushes, 1)
        finally:
            event.remove(self.database, "after_flush", count_flush)
        monitor.finish_branch()
