from __future__ import annotations

from dataclasses import FrozenInstanceError

from spacetimepy import TraceNotFoundError
from tests.support import SpaceTimeTestCase, assert_dto


class TestTraceData(SpaceTimeTestCase):
    def record_call(self, value: int = 2, payload: object = None):
        @self.space.capture.function
        def calculate(value: int, payload: object) -> int:
            return value + 1

        with self.space.capture.recording(name="data") as recording:
            calculate(value, payload)
        return recording, self.space.data.get_branch(recording.branch_id).steps[0]

    def test_public_queries_return_dtos_not_orm_entities(self) -> None:
        recording, step = self.record_call(payload=[1, 2, 3])

        session = self.space.data.get_session(recording.session_id)
        branch = self.space.data.get_branch(recording.branch_id)
        call = self.space.data.get_function_call(step.function_call.id)
        snapshot_free_step = self.space.data.get_step(step.id)

        for value in (session, branch, call, snapshot_free_step):
            assert_dto(self, value)
        with self.assertRaises(FrozenInstanceError):
            session.name = "changed"

    def test_session_listing_contains_root_and_branch_summaries(self) -> None:
        first, _ = self.record_call(1)
        second, _ = self.record_call(2)

        sessions = self.space.data.list_sessions()

        self.assertEqual([session.id for session in sessions], [first.session_id, second.session_id])
        self.assertEqual(sessions[0].root_branch_id, first.branch_id)
        self.assertEqual(sessions[0].branches[0].own_step_count, 1)

    def test_stored_values_and_reference_mappings_are_materialized(self) -> None:
        payload = [1, {"nested": True}]
        _, step = self.record_call(value=5, payload=payload)
        references = step.function_call.entry_local_references

        loaded = self.space.data.load_references(references)
        stored_payload = self.space.data.get_stored_value(references["payload"])

        self.assertEqual(loaded, {"value": 5, "payload": payload})
        self.assertEqual(stored_payload.value, payload)
        self.assertFalse(stored_payload.is_primitive)
        self.assertEqual(
            self.space.data.load_values(references.values()),
            {
                references["value"]: 5,
                references["payload"]: payload,
            },
        )

    def test_code_definition_is_available_from_a_call_dto(self) -> None:
        _, step = self.record_call()
        definition_id = step.function_call.code_definition_id

        self.assertIsNotNone(definition_id)
        definition = self.space.data.get_code_definition(definition_id)
        self.assertEqual(definition.name, "calculate")
        self.assertIn("def calculate", definition.code_content)

    def test_resolved_child_path_replaces_the_fork_step_and_later_suffix(self) -> None:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value + 1

        with self.space.capture.recording(name="root") as recording:
            calculate(1)
            calculate(2)
            calculate(3)
        root = self.space.data.get_branch(recording.branch_id)
        fork = root.steps[1]

        @self.space.capture.function
        def changed(value: int) -> int:
            return value + 10

        result = self.space.replay.run(
            lambda context: changed(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=fork.id,
        )
        resolved = self.space.data.get_branch(result.branch.id, resolve=True)

        self.assertEqual(
            [step.id for step in resolved.steps],
            [root.steps[0].id, result.branch.steps[0].id],
        )
        self.assertNotIn(fork.id, [step.id for step in resolved.steps])
        self.assertNotIn(root.steps[2].id, [step.id for step in resolved.steps])
        self.assertTrue(resolved.is_resolved_path)

    def test_own_suffix_and_resolved_path_are_explicitly_distinguished(self) -> None:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value

        with self.space.capture.recording() as recording:
            calculate(1)
            calculate(2)
        root = self.space.data.get_branch(recording.branch_id)

        result = self.space.replay.run(
            lambda context: calculate(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=root.steps[1].id,
        )
        suffix = self.space.data.get_branch(result.branch.id)
        resolved = self.space.data.get_branch(result.branch.id, resolve=True)

        self.assertFalse(suffix.is_resolved_path)
        self.assertEqual(len(suffix.steps), 1)
        self.assertEqual(len(resolved.steps), 2)

    def test_missing_entities_and_references_have_specific_errors(self) -> None:
        with self.assertRaisesRegex(TraceNotFoundError, "execution session"):
            self.space.data.get_session(999)
        with self.assertRaisesRegex(TraceNotFoundError, "stored value"):
            self.space.data.load_value("missing")
        with self.assertRaisesRegex(TraceNotFoundError, "missing"):
            self.space.data.load_values(["missing"])
