from __future__ import annotations

from spacetimepy import ReplayDivergenceError, ReplayError
from spacetimepy.core.monitoring import SpaceTimeMonitor
from tests.support import SpaceTimeTestCase


class TestReplayInterface(SpaceTimeTestCase):
    def record_external_step(self):
        @self.space.capture.external
        def read_external(value: int) -> int:
            return value * 2

        @self.space.capture.function
        def calculate(value: int) -> int:
            return read_external(value) + 1

        with self.space.capture.recording(name="root") as recording:
            calculate(3)
        branch = self.space.data.get_branch(recording.branch_id)
        return recording, branch, branch.steps[0], read_external

    def test_prepare_materializes_exact_state_without_creating_a_branch(self) -> None:
        recording, branch, source, _ = self.record_external_step()
        before = len(self.space.data.get_session(recording.session_id).branches)

        context = self.space.replay.prepare(
            parent_branch_id=branch.id,
            forked_from_step_id=source.id,
            recipe={"code": "changed"},
            options={"mock_external": True},
        )

        after = len(self.space.data.get_session(recording.session_id).branches)
        self.assertEqual(context.branch_id, -1)
        self.assertEqual(context.locals, {"value": 3})
        self.assertEqual(context.recipe, {"code": "changed"})
        self.assertEqual(context.options, {"mock_external": True})
        self.assertEqual(before, after)

    def test_external_script_validates_order_and_can_build_a_mock(self) -> None:
        _, branch, source, read_external = self.record_external_step()
        context = self.space.replay.prepare(
            parent_branch_id=branch.id,
            forked_from_step_id=source.id,
        )

        with self.assertRaises(ReplayDivergenceError):
            context.external.take("different.function")
        self.assertEqual(context.external.remaining, 1)

        mocked = context.external.mock(read_external)
        self.assertEqual(mocked(999), 6)
        context.external.assert_consumed()
        with self.assertRaisesRegex(ReplayDivergenceError, "exhausted"):
            context.external.take(read_external)

    def test_external_script_accepts_the_original_name_of_a_replay_mock(self) -> None:
        @self.space.capture.external(
            start_hooks=(
                lambda _context: {
                    "replay_target_names": ["random.randint"],
                },
            )
        )
        def replacement() -> int:
            return 7

        @self.space.capture.function
        def calculate() -> int:
            return replacement()

        with self.space.capture.recording() as recording:
            calculate()
        source = self.space.data.get_branch(recording.branch_id).steps[0]
        context = self.space.replay.prepare(
            parent_branch_id=recording.branch_id,
            forked_from_step_id=source.id,
        )

        self.assertEqual(context.external.take("random.randint"), 7)

    def test_raised_external_interaction_is_reraised_by_script(self) -> None:
        @self.space.capture.external
        def unstable() -> None:
            raise ValueError("recorded failure")

        @self.space.capture.function
        def guarded() -> str:
            try:
                unstable()
            except ValueError:
                return "handled"
            return "unexpected"

        with self.space.capture.recording() as recording:
            self.assertEqual(guarded(), "handled")
        source = self.space.data.get_branch(recording.branch_id).steps[0]
        context = self.space.replay.prepare(
            parent_branch_id=recording.branch_id,
            forked_from_step_id=source.id,
        )

        with self.assertRaisesRegex(ValueError, "recorded failure"):
            context.external.take(unstable)
        context.external.assert_consumed()

    def test_manual_begin_and_finish_support_asynchronous_integrations(self) -> None:
        _, root, source, _ = self.record_external_step()

        @self.space.capture.function
        def changed(value: int) -> int:
            return value + 10

        context = self.space.replay.begin(
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
            name="variant",
            configuration_key="v2",
            recipe={"code": "changed"},
            options={"debugger": "attached"},
        )
        self.assertEqual(changed(context.locals["value"]), 13)
        branch = self.space.replay.finish()

        self.assertEqual(branch.name, "variant")
        self.assertEqual(branch.configuration_key, "v2")
        self.assertEqual(branch.recipe, {"code": "changed"})
        self.assertEqual(context.options, {"debugger": "attached"})
        self.assertNotIn("debugger", branch.recipe)
        self.assertEqual(branch.steps[0].source_step_id, source.id)

    def test_run_returns_executor_value_and_completed_branch(self) -> None:
        _, root, source, _ = self.record_external_step()

        @self.space.capture.function
        def changed(value: int) -> int:
            return value + 20

        result = self.space.replay.run(
            lambda context: changed(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
        )

        self.assertEqual(result.value, 23)
        self.assertEqual(result.branch.status, "completed")
        self.assertEqual(result.branch.steps[0].source_step_id, source.id)

    def test_successful_replay_requires_a_real_replacement_step(self) -> None:
        _, root, source, _ = self.record_external_step()
        context = self.space.replay.begin(
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
        )

        with self.assertRaisesRegex(ReplayError, "replacement step"):
            self.space.replay.finish()

        branch = self.space.data.get_branch(context.branch_id)
        self.assertEqual(branch.status, "failed")
        self.assertEqual(branch.steps, ())
        self.assertIsNone(SpaceTimeMonitor.get_instance().current_branch)

    def test_executor_failure_marks_branch_failed_and_releases_monitor(self) -> None:
        recording, root, source, _ = self.record_external_step()

        def fail(context: object) -> None:
            del context
            raise RuntimeError("migration failed")

        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            self.space.replay.run(
                fail,
                parent_branch_id=root.id,
                forked_from_step_id=source.id,
            )

        session = self.space.data.get_session(recording.session_id)
        child = next(branch for branch in session.branches if branch.parent_branch_id)
        self.assertEqual(child.status, "failed")
        self.assertIsNone(SpaceTimeMonitor.get_instance().current_branch)

    def test_required_external_consumption_detects_replay_divergence(self) -> None:
        recording, root, source, _ = self.record_external_step()

        @self.space.capture.function
        def changed(value: int) -> int:
            return value + 10

        with self.assertRaisesRegex(ReplayDivergenceError, "remaining"):
            self.space.replay.run(
                lambda context: changed(context.locals["value"]),
                parent_branch_id=root.id,
                forked_from_step_id=source.id,
                require_external_consumption=True,
            )

        session = self.space.data.get_session(recording.session_id)
        child = next(branch for branch in session.branches if branch.parent_branch_id)
        self.assertEqual(child.status, "failed")

    def test_fork_step_must_belong_to_selected_parent_path(self) -> None:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value

        with self.space.capture.recording(name="first") as first:
            calculate(1)
        with self.space.capture.recording(name="second") as second:
            calculate(2)
        unrelated_step = self.space.data.get_branch(second.branch_id).steps[0]

        with self.assertRaisesRegex(ReplayError, "not in the resolved path"):
            self.space.replay.prepare(
                parent_branch_id=first.branch_id,
                forked_from_step_id=unrelated_step.id,
            )

    def test_snapshot_replay_state_is_taken_at_the_selected_line(self) -> None:
        def calculate(value: int) -> int:
            value += 1
            value *= 2
            return value

        selected_line = calculate.__code__.co_firstlineno + 2
        calculate = self.space.capture.line(lines={selected_line})(calculate)
        with self.space.capture.recording(mode="line") as recording:
            calculate(2)
        source = self.space.data.get_branch(recording.branch_id).steps[0]

        context = self.space.replay.prepare(
            parent_branch_id=recording.branch_id,
            forked_from_step_id=source.id,
        )

        self.assertEqual(context.locals["value"], 3)
