from __future__ import annotations

from sqlalchemy import select

from spacetimepy.core.model import (
    FunctionCall,
    FunctionCallCapturePerformance,
    StackSnapshot,
    StepKind,
)
from spacetimepy.core.monitoring import CallRole
from tests.support import DatabaseTestCase


class IncrementingClock:
    def __init__(self, increment_ns: int = 10) -> None:
        self.increment_ns = increment_ns
        self.current_ns = 0
        self.call_count = 0

    def __call__(self) -> int:
        value = self.current_ns
        self.current_ns += self.increment_ns
        self.call_count += 1
        return value


class TestCapturePerformance(DatabaseTestCase):
    def test_profiling_off_creates_no_performance_rows(self) -> None:
        monitor = self.create_monitor(profile_capture=False)
        self.start_branch(monitor)

        def calculate(value: int) -> int:
            return value + 1

        monitor.register_capture(calculate, role=CallRole.STEP)
        self.assertEqual(calculate(2), 3)
        monitor.finish_branch()

        self.assertIsNone(monitor.capture_profiler)
        self.assertEqual(
            self.database.scalars(select(FunctionCallCapturePerformance)).all(),
            [],
        )

    def test_function_profile_is_persisted_after_the_timed_callbacks(self) -> None:
        monitor = self.create_monitor(profile_capture=True)
        clock = IncrementingClock()
        monitor.capture_profiler._clock = clock
        self.start_branch(monitor)

        def calculate(value: int) -> int:
            return value + 1

        monitor.register_capture(calculate, role=CallRole.STEP)
        self.assertEqual(calculate(2), 3)

        # Metrics remain in memory while capture is active, ensuring their ORM
        # persistence cannot become part of another timed callback.
        self.assertEqual(
            self.database.scalars(select(FunctionCallCapturePerformance)).all(),
            [],
        )
        monitor.finish_branch()

        performance = self.database.scalars(
            select(FunctionCallCapturePerformance)
        ).one()
        self.assertEqual(performance.start_capture_ns, 10)
        self.assertEqual(performance.return_capture_ns, 10)
        self.assertEqual(performance.unwind_capture_ns, 0)
        self.assertEqual(performance.direct_capture_ns, 20)
        self.assertEqual(performance.inclusive_capture_ns, 20)
        self.assertEqual(performance.line_event_count, 0)
        self.assertEqual(clock.call_count, 4)

    def test_nested_call_profile_separates_direct_and_inclusive_cost(self) -> None:
        monitor = self.create_monitor(profile_capture=True)
        clock = IncrementingClock()
        monitor.capture_profiler._clock = clock
        self.start_branch(monitor)

        def helper(value: int) -> int:
            return value * 2

        def calculate(value: int) -> int:
            return helper(value) + 1

        monitor.register_capture(helper, role=CallRole.SUPPORT)
        monitor.register_capture(calculate, role=CallRole.STEP)
        self.assertEqual(calculate(3), 7)
        monitor.finish_branch()

        calls = {
            call.function_name: call
            for call in self.database.scalars(select(FunctionCall)).all()
        }
        self.assertEqual(calls["helper"].capture_performance.direct_capture_ns, 20)
        self.assertEqual(calls["helper"].capture_performance.inclusive_capture_ns, 20)
        self.assertEqual(calls["calculate"].capture_performance.direct_capture_ns, 20)
        self.assertEqual(
            calls["calculate"].capture_performance.inclusive_capture_ns, 40
        )

    def test_line_mode_aggregates_line_measurements_on_the_call(self) -> None:
        monitor = self.create_monitor(profile_capture=True)
        clock = IncrementingClock()
        monitor.capture_profiler._clock = clock
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

        performance = self.database.scalars(
            select(FunctionCallCapturePerformance)
        ).one()
        snapshot_count = len(self.database.scalars(select(StackSnapshot)).all())
        self.assertEqual(snapshot_count, 1)
        self.assertEqual(performance.line_snapshot_count, snapshot_count)
        self.assertGreater(performance.line_event_count, snapshot_count)
        self.assertEqual(
            performance.filtered_line_event_count,
            performance.line_event_count - snapshot_count,
        )
        self.assertEqual(
            performance.line_capture_ns,
            performance.line_event_count * clock.increment_ns,
        )
        self.assertEqual(performance.line_capture_min_ns, clock.increment_ns)
        self.assertEqual(performance.line_capture_max_ns, clock.increment_ns)

    def test_line_limit_counts_ignored_callbacks_without_extra_snapshots(self) -> None:
        monitor = self.create_monitor(profile_capture=True)
        clock = IncrementingClock()
        monitor.capture_profiler._clock = clock
        self.start_branch(monitor, kind=StepKind.STACK_SNAPSHOT)

        def calculate(iterations: int) -> int:
            total = 0
            for index in range(iterations):
                total += index
            return total

        selected_line = calculate.__code__.co_firstlineno + 3
        monitor.register_capture(
            calculate,
            role=CallRole.STEP,
            capture_lines=True,
            line_numbers={selected_line},
            max_snapshots_per_line=2,
        )
        self.assertEqual(calculate(6), 15)
        monitor.finish_branch()

        call = self.database.scalars(select(FunctionCall)).one()
        performance = self.database.scalars(
            select(FunctionCallCapturePerformance)
        ).one()
        snapshots = self.database.scalars(select(StackSnapshot)).all()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(performance.line_snapshot_count, 2)
        self.assertEqual(
            performance.filtered_line_event_count,
            performance.line_event_count - 2,
        )
        summary = call.attributes["line_capture_limit"]
        self.assertEqual(summary["lines"][0]["captured"], 2)
        self.assertEqual(summary["lines"][0]["ignored"], 4)

    def test_unwind_cost_is_kept_separate_from_normal_return(self) -> None:
        monitor = self.create_monitor(profile_capture=True)
        clock = IncrementingClock()
        monitor.capture_profiler._clock = clock
        self.start_branch(monitor)

        def explode() -> None:
            raise ValueError("boom")

        monitor.register_capture(explode, role=CallRole.STEP)
        with self.assertRaisesRegex(ValueError, "boom"):
            explode()
        monitor.finish_branch()

        performance = self.database.scalars(
            select(FunctionCallCapturePerformance)
        ).one()
        self.assertEqual(performance.return_capture_ns, 0)
        self.assertEqual(performance.unwind_capture_ns, 10)
        self.assertEqual(performance.direct_capture_ns, 20)
