from __future__ import annotations

import unittest

from spacetimepy import SpaceTime
from tests.support import SpaceTimeTestCase


class TestPublicWorkflows(SpaceTimeTestCase):
    def test_function_capture_query_external_mock_and_replay(self) -> None:
        @self.space.capture.external
        def external_value(value: int) -> int:
            return value * 2

        @self.space.capture.function
        def calculate(value: int) -> int:
            return external_value(value) + 1

        with self.space.capture.recording(name="root") as recording:
            self.assertEqual(calculate(3), 7)

        root = self.space.data.get_branch(recording.branch_id, resolve=True)
        self.assertEqual(len(root.steps), 1)
        source = root.steps[0]
        self.assertEqual(len(source.external_interactions), 1)
        self.assertEqual(
            self.space.data.load_references(
                source.function_call.entry_local_references
            ),
            {"value": 3},
        )

        prepared = self.space.replay.prepare(
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
        )
        mocked_external = prepared.external.mock(external_value)
        self.assertEqual(mocked_external(100), 6)
        prepared.external.assert_consumed()

        @self.space.capture.function
        def recalculate(value: int) -> int:
            return value + 10

        result = self.space.replay.run(
            lambda context: recalculate(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
            recipe={"code": "recalculate"},
        )
        child = self.space.data.get_branch(result.branch.id, resolve=True)
        self.assertEqual(result.value, 13)
        self.assertEqual(child.steps[-1].source_step_id, source.id)
        self.assertEqual(
            child.steps[-1].function_call.function_name,
            "recalculate",
        )

    def test_line_capture_query_and_snapshot_replay(self) -> None:
        def increment(value: int) -> int:
            value += 1
            return value

        selected_line = increment.__code__.co_firstlineno + 1
        increment = self.space.capture.line(lines={selected_line})(increment)
        with self.space.capture.recording(mode="line") as recording:
            self.assertEqual(increment(4), 5)

        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual(len(branch.steps), 1)
        self.assertEqual(branch.steps[0].kind, "stack_snapshot")

        replay_input = self.space.replay.prepare(
            parent_branch_id=branch.id,
            forked_from_step_id=branch.steps[0].id,
        )
        self.assertEqual(replay_input.locals["value"], 4)

    def test_tree_contains_root_and_independent_variants(self) -> None:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value + 1

        with self.space.capture.recording(name="tree") as recording:
            calculate(1)
            calculate(2)
            calculate(3)
        root = self.space.data.get_branch(recording.branch_id)
        fork = root.steps[1]

        @self.space.capture.function
        def variant_a(value: int) -> int:
            return value + 10

        @self.space.capture.function
        def variant_b(value: int) -> int:
            return value + 20

        first = self.space.replay.run(
            lambda context: variant_a(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=fork.id,
            name="A",
        )
        second = self.space.replay.run(
            lambda context: variant_b(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=fork.id,
            name="B",
        )

        session = self.space.data.get_session(recording.session_id)
        refreshed_root = self.space.data.get_branch(root.id)
        self.assertEqual(len(session.branches), 3)
        self.assertEqual(
            set(refreshed_root.child_branch_ids),
            {first.branch.id, second.branch.id},
        )
        self.assertEqual(
            [step.function_call.function_name for step in self.space.data.get_branch(first.branch.id, resolve=True).steps],
            ["calculate", "variant_a"],
        )
        self.assertEqual(
            [step.function_call.function_name for step in self.space.data.get_branch(second.branch.id, resolve=True).steps],
            ["calculate", "variant_b"],
        )


class TestPublicFactoryWorkflow(unittest.TestCase):
    """Exercise runtime construction without the shared fixture."""

    def test_context_managed_runtime(self) -> None:
        with SpaceTime.open() as space:
            self.assertFalse(space.is_closed)
