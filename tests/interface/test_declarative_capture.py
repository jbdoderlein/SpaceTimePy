from __future__ import annotations

import random

import spacetimepy
from spacetimepy import CaptureMode, SpaceTime
from spacetimepy.core.monitoring import CallRole, SpaceTimeMonitor
from spacetimepy.interface.capture import capture_registry
from tests.support import SpaceTimeTestCase


class TestDeclarativeCapture(SpaceTimeTestCase):
    def test_function_can_be_declared_and_called_before_runtime_exists(self) -> None:
        self.space.close()

        @spacetimepy.function
        def calculate(value: int) -> int:
            return value + 1

        self.assertEqual(calculate(1), 2)
        self.assertEqual(len(capture_registry.declarations), 1)

        self.space = SpaceTime.open()
        with self.space.capture.recording() as recording:
            self.assertEqual(calculate(3), 4)

        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual(len(branch.steps), 1)
        self.assertEqual(branch.steps[0].function_call.function_name, "calculate")

    def test_line_declaration_is_installed_when_runtime_opens(self) -> None:
        self.space.close()

        def calculate(value: int) -> int:
            value += 1
            value *= 2
            return value

        selected_line = calculate.__code__.co_firstlineno + 2
        calculate = spacetimepy.line(lines={selected_line})(calculate)

        self.space = SpaceTime.open()
        with self.space.capture.recording(mode=CaptureMode.LINE) as recording:
            self.assertEqual(calculate(2), 6)

        steps = self.space.data.get_branch(recording.branch_id).steps
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].stack_snapshot.line_number, selected_line)

    def test_line_declaration_keeps_snapshot_limit_before_runtime_opens(self) -> None:
        self.space.close()

        def calculate(iterations: int) -> int:
            total = 0
            for index in range(iterations):
                total += index
            return total

        selected_line = calculate.__code__.co_firstlineno + 3
        calculate = spacetimepy.line(
            lines={selected_line},
            max_snapshots_per_line=2,
        )(calculate)

        self.space = SpaceTime.open()
        with self.space.capture.recording(mode=CaptureMode.LINE) as recording:
            self.assertEqual(calculate(5), 10)

        steps = self.space.data.get_branch(recording.branch_id).steps
        self.assertEqual(len(steps), 2)
        call = self.space.data.get_function_call(
            steps[0].stack_snapshot.function_call_id
        )
        summary = call.attributes["line_capture_limit"]
        self.assertEqual(summary["lines"][0]["ignored"], 3)

    def test_line_declaration_rejects_invalid_snapshot_limit_immediately(self) -> None:
        self.space.close()

        with self.assertRaisesRegex(TypeError, "must be an integer or None"):
            spacetimepy.line(max_snapshots_per_line=1.5)

    def test_declaration_made_after_runtime_open_is_installed_immediately(self) -> None:
        @spacetimepy.function
        def calculate() -> int:
            return 4

        monitor = SpaceTimeMonitor.get_instance()
        registration = next(
            item for item in monitor.captures if item.code is calculate.__code__
        )
        self.assertEqual(registration.role, CallRole.STEP)

        with self.space.capture.recording() as recording:
            calculate()
        self.assertEqual(len(self.space.data.get_branch(recording.branch_id).steps), 1)

    def test_global_support_and_external_decorators_install_their_roles(self) -> None:
        @spacetimepy.support
        def helper() -> int:
            return 1

        @spacetimepy.external
        def read_input() -> int:
            return 2

        roles = {
            registration.code: registration.role
            for registration in SpaceTimeMonitor.get_instance().captures
        }
        self.assertEqual(roles[helper.__code__], CallRole.SUPPORT)
        self.assertEqual(roles[read_input.__code__], CallRole.EXTERNAL_INTERACTION)

    def test_declaration_survives_runtime_reopen(self) -> None:
        self.space.close()

        @spacetimepy.function
        def calculate(value: int) -> int:
            return value + 1

        self.space = SpaceTime.open()
        with self.space.capture.recording() as first:
            calculate(1)
        self.assertEqual(len(self.space.data.get_branch(first.branch_id).steps), 1)
        self.space.close()

        self.space = SpaceTime.open()
        with self.space.capture.recording() as second:
            calculate(2)
        self.assertEqual(len(self.space.data.get_branch(second.branch_id).steps), 1)

    def test_external_declaration_is_automatically_linked_to_a_step(self) -> None:
        @spacetimepy.external
        def read_input(value: int) -> int:
            return value * 2

        @spacetimepy.function
        def calculate(value: int) -> int:
            return read_input(value) + 1

        with self.space.capture.recording() as recording:
            self.assertEqual(calculate(3), 7)

        step = self.space.data.get_branch(recording.branch_id).steps[0]
        self.assertEqual(
            [item.call.function_name for item in step.external_interactions],
            ["read_input"],
        )

    def test_global_unregister_removes_declaration_and_active_events(self) -> None:
        @spacetimepy.function
        def calculate() -> int:
            return 1

        self.assertTrue(spacetimepy.unregister_capture_declaration(calculate))
        self.assertFalse(spacetimepy.unregister_capture_declaration(calculate))
        self.assertEqual(capture_registry.declarations, ())
        self.assertFalse(
            any(
                registration.code is calculate.__code__
                for registration in SpaceTimeMonitor.get_instance().captures
            )
        )

    def test_capture_interface_clear_also_removes_global_declarations(self) -> None:
        @spacetimepy.function
        def calculate() -> int:
            return 1

        self.space.capture.clear()

        self.assertEqual(capture_registry.declarations, ())
        captures = {
            registration.code: registration.role
            for registration in SpaceTimeMonitor.get_instance().captures
        }
        self.assertNotIn(calculate.__code__, captures)
        self.assertEqual(
            captures[random.randint.__func__.__code__],
            CallRole.EXTERNAL_INTERACTION,
        )
