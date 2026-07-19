from __future__ import annotations

import random

from spacetimepy import CaptureMode
from spacetimepy.core.monitoring import CallRole, SpaceTimeMonitor
from tests.support import SpaceTimeTestCase


class TestCaptureInterface(SpaceTimeTestCase):
    def test_decorators_return_the_original_callable_and_register_roles(self) -> None:
        def step() -> None:
            return None

        def support() -> None:
            return None

        def external() -> None:
            return None

        self.assertIs(self.space.capture.function(step), step)
        self.assertIs(self.space.capture.support(support), support)
        self.assertIs(self.space.capture.external(external), external)

        monitor = SpaceTimeMonitor.get_instance()
        roles = {registration.code: registration.role for registration in monitor.captures}
        self.assertEqual(roles[step.__code__], CallRole.STEP)
        self.assertEqual(roles[support.__code__], CallRole.SUPPORT)
        self.assertEqual(roles[external.__code__], CallRole.EXTERNAL_INTERACTION)

    def test_recording_context_creates_named_session_and_root_branch(self) -> None:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value + 1

        with self.space.capture.recording(
            name="calculation",
            description="root execution",
            branch_name="baseline",
            attributes={"owner": "test"},
            branch_attributes={"variant": "original"},
        ) as recording:
            self.assertEqual(calculate(2), 3)

        session = self.space.data.get_session(recording.session_id)
        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual(recording.mode, CaptureMode.FUNCTION)
        self.assertEqual(session.name, "calculation")
        self.assertEqual(session.description, "root execution")
        self.assertEqual(session.attributes, {"owner": "test"})
        self.assertEqual(session.root_branch_id, branch.id)
        self.assertEqual(branch.name, "baseline")
        self.assertEqual(branch.attributes, {"variant": "original"})
        self.assertEqual(session.status, "completed")
        self.assertEqual(branch.status, "completed")

    def test_integrations_can_annotate_sessions_and_branches(self) -> None:
        with self.space.capture.recording(
            attributes={"existing": "session"},
            branch_attributes={"existing": "branch"},
        ) as recording:
            pass

        self.space.capture.annotate_session(
            recording.session_id,
            {"baseline": "variant-a"},
        )
        self.space.capture.annotate_branch(
            recording.branch_id,
            {"summary": [1, 2, 3]},
        )

        session = self.space.data.get_session(recording.session_id)
        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual(
            session.attributes,
            {"existing": "session", "baseline": "variant-a"},
        )
        self.assertEqual(
            branch.attributes,
            {"existing": "branch", "summary": [1, 2, 3]},
        )

    def test_disabled_context_skips_calls_without_changing_positions(self) -> None:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value + 1

        with self.space.capture.recording() as recording:
            calculate(1)
            with self.space.capture.disabled():
                self.assertFalse(self.space.capture.is_enabled)
                calculate(2)
            self.assertTrue(self.space.capture.is_enabled)
            calculate(3)

        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual([step.position for step in branch.steps], [0, 1])
        self.assertEqual(
            [
                self.space.data.load_references(
                    step.function_call.entry_local_references
                )["value"]
                for step in branch.steps
            ],
            [1, 3],
        )

    def test_ignored_names_are_not_stored(self) -> None:
        @self.space.capture.function(ignored_names={"secret"})
        def calculate(value: int, secret: str) -> int:
            return value + len(secret)

        with self.space.capture.recording() as recording:
            calculate(2, "hidden")

        call = self.space.data.get_branch(recording.branch_id).steps[0].function_call
        self.assertIn("value", call.entry_local_references)
        self.assertNotIn("secret", call.entry_local_references)

    def test_external_decorator_records_through_unmonitored_helpers(self) -> None:
        @self.space.capture.external
        def read_input(value: int) -> int:
            return value * 2

        def helper(value: int) -> int:
            return read_input(value)

        @self.space.capture.function
        def calculate(value: int) -> int:
            return helper(value) + 1

        with self.space.capture.recording() as recording:
            self.assertEqual(calculate(3), 7)

        step = self.space.data.get_branch(recording.branch_id).steps[0]
        self.assertEqual(len(step.external_interactions), 1)
        self.assertEqual(
            step.external_interactions[0].call.function_name,
            "read_input",
        )
        self.assertEqual(
            step.external_interactions[0].call.caller_call_id,
            step.function_call.id,
        )

    def test_line_decorator_supports_selected_lines_and_attributes(self) -> None:
        def line_attributes(*args: object) -> dict[str, object]:
            line_number = args[-1]
            return {"selected": line_number}

        def calculate(value: int) -> int:
            value += 1
            value *= 2
            return value

        selected_line = calculate.__code__.co_firstlineno + 2
        calculate = self.space.capture.line(
            lines={selected_line},
            line_attributes=line_attributes,
        )(calculate)
        with self.space.capture.recording(mode=CaptureMode.LINE) as recording:
            self.assertEqual(calculate(2), 6)

        steps = self.space.data.get_branch(recording.branch_id).steps
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].stack_snapshot.line_number, selected_line)
        self.assertEqual(
            steps[0].stack_snapshot.attributes["selected"],
            selected_line,
        )

    def test_uncaught_program_exception_marks_recording_failed(self) -> None:
        @self.space.capture.function
        def explode() -> None:
            raise LookupError("failure")

        recording = None
        with (
            self.assertRaisesRegex(LookupError, "failure"),
            self.space.capture.recording() as active_recording,
        ):
            recording = active_recording
            explode()

        session = self.space.data.get_session(recording.session_id)
        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual(session.status, "failed")
        self.assertEqual(branch.status, "failed")
        self.assertEqual(branch.steps[0].function_call.outcome, "raised")

    def test_nested_recording_and_invalid_mode_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.space.capture.begin_recording(mode="instruction")

        handle = self.space.capture.begin_recording()
        try:
            with self.assertRaises(RuntimeError):
                self.space.capture.begin_recording()
        finally:
            finished = self.space.capture.finish_recording("cancelled")
        self.assertEqual(finished, handle)

    def test_unregister_and_clear_remove_transient_declarations(self) -> None:
        @self.space.capture.function
        def first() -> None:
            return None

        @self.space.capture.function
        def second() -> None:
            return None

        self.assertTrue(self.space.capture.unregister(first))
        self.assertFalse(self.space.capture.unregister(first))
        self.space.capture.clear()
        captures = {
            registration.code: registration.role
            for registration in SpaceTimeMonitor.get_instance().captures
        }
        self.assertNotIn(first.__code__, captures)
        self.assertNotIn(second.__code__, captures)
        self.assertEqual(
            captures[random.randint.__func__.__code__],
            CallRole.EXTERNAL_INTERACTION,
        )
