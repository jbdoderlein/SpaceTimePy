from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from spacetimepy import (
    AlignmentLink,
    AlignmentRelation,
    ReplayAlignmentPolicy,
    ReplayDivergenceError,
    ReplayError,
)
from spacetimepy.core.monitoring import SpaceTimeMonitor
from tests.support import SpaceTimeTestCase

if TYPE_CHECKING:
    from spacetimepy import OnlineAlignmentContext, StepDTO


class _ReplayAlignmentSession:
    def __init__(self, context: OnlineAlignmentContext) -> None:
        self._reference_steps = context.reference_steps
        self._position = 0

    def align(self, target_step: StepDTO) -> tuple[AlignmentLink, ...]:
        reference_step = self._reference_steps[self._position]
        self._position += 1
        return (
            AlignmentLink(
                reference_step,
                target_step,
                AlignmentRelation.MATCH,
            ),
        )

    def finish(self) -> tuple[AlignmentLink, ...]:
        return tuple(
            AlignmentLink(step, None, AlignmentRelation.DELETED)
            for step in self._reference_steps[self._position :]
        )


class _ReplayAlignment:
    name = "replay-position"
    version = "1"

    def start(self, context: OnlineAlignmentContext) -> _ReplayAlignmentSession:
        return _ReplayAlignmentSession(context)


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

    def test_active_external_runs_real_effect_and_records_mocked_outcome(
        self,
    ) -> None:
        effects: list[int] = []

        @self.space.capture.external
        def read_external(value: int) -> int:
            effects.append(value)
            return value * 2

        @self.space.capture.function
        def original(value: int) -> int:
            return read_external(value) + 1

        with self.space.capture.recording() as recording:
            self.assertEqual(original(3), 7)
        root = self.space.data.get_branch(recording.branch_id)
        source = root.steps[0]

        def execute(context):
            active_external = self.space.capture.external(
                context.external.active(read_external)
            )

            @self.space.capture.function
            def changed(value: int) -> int:
                return active_external(value) + 1

            try:
                return changed(100)
            finally:
                self.space.capture.unregister(changed)
                self.space.capture.unregister(active_external)

        result = self.space.replay.run(
            execute,
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
            require_external_consumption=True,
        )
        child = self.space.data.get_branch(result.branch.id, resolve=True)

        self.assertEqual(effects, [3, 100])
        self.assertEqual(result.value, 7)
        self.assertEqual(len(child.steps[-1].external_interactions), 1)
        replayed_call = child.steps[-1].external_interactions[0].call
        self.assertEqual(
            self.space.data.load_value(replayed_call.return_reference),
            6,
        )

    def test_active_external_propagates_real_failure_without_consuming_record(
        self,
    ) -> None:
        @self.space.capture.external
        def read_external(value: int) -> int:
            if value < 0:
                raise RuntimeError("real call failed")
            return value * 2

        @self.space.capture.function
        def original(value: int) -> int:
            return read_external(value)

        with self.space.capture.recording() as recording:
            original(3)
        source = self.space.data.get_branch(recording.branch_id).steps[0]
        context = self.space.replay.prepare(
            parent_branch_id=recording.branch_id,
            forked_from_step_id=source.id,
        )

        active_external = context.external.active(read_external)
        with self.assertRaisesRegex(RuntimeError, "real call failed"):
            active_external(-1)
        self.assertEqual(context.external.remaining, 1)

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

    def test_active_execution_moves_to_replacement_frame_and_child_branch(self) -> None:
        replay_context = None
        replacement_line = None

        def replacement(source_frame, value: int) -> int:
            nonlocal replay_context, replacement_line
            replacement_frame = inspect.currentframe()
            assert replacement_frame is not None
            replay_context = self.space.replay.begin_active_execution(
                source_frame=source_frame,
                replacement_frame=replacement_frame,
                replacement_target=replacement,
                replacement_line_numbers=(),
                name="edited code",
                recipe={"integration": "test-hotswap"},
            )
            # debugpy can emit PY_START again when set-next-statement redirects
            # this already transferred frame. The recorder must treat it as
            # the same logical call instead of disabling all later line events.
            monitor = SpaceTimeMonitor.get_instance()
            assert monitor is not None
            monitor._monitor_callback_function_start(replacement.__code__, 0)
            self.assertIsNone(monitor.last_callback_error)
            self.assertTrue(monitor.is_recording_enabled)
            replacement_line = replacement_frame.f_lineno + 1
            self.space.replay.record_active_replacement_state(
                frame=replacement_frame,
                line_number=replacement_line,
                replacement_line_numbers={replacement_line},
            )
            # VS Code commits at each debugger stop so the explorer API can
            # read the active branch from its separate process.
            self.space.commit()
            value += 10
            return value

        def original(value: int) -> int:
            value += 1
            source_frame = inspect.currentframe()
            assert source_frame is not None
            return replacement(source_frame, value)

        original = self.space.capture.line(original)
        with self.space.capture.recording(mode="line") as recording:
            self.assertEqual(original(2), 13)
            child = self.space.replay.finish()

        assert replay_context is not None
        root = self.space.data.get_branch(recording.branch_id)
        session = self.space.data.get_session(recording.session_id)
        self.assertEqual(child.parent_branch_id, root.id)
        self.assertEqual(child.forked_from_step_id, replay_context.forked_from_step.id)
        self.assertEqual(child.steps[0].source_step_id, child.forked_from_step_id)
        self.assertEqual(child.recipe, {"integration": "test-hotswap"})
        self.assertEqual(root.status, "completed")
        self.assertEqual(session.status, "completed")
        replacement_snapshot = child.steps[0].stack_snapshot
        assert replacement_snapshot is not None
        assert replacement_line is not None
        self.assertEqual(replacement_snapshot.line_number, replacement_line)
        assert replacement_snapshot.code_definition_id is not None
        replacement_code = self.space.data.get_code_definition(
            replacement_snapshot.code_definition_id
        )
        self.assertIn("replacement", replacement_code.qualified_name)

    def test_active_execution_can_refork_from_a_historical_checkpoint(self) -> None:
        first_context = None
        checkpoint_context = None
        historical_step_id = None

        def checkpoint_replacement(
            source_frame,
            value: int,
            parent_branch_id: int,
            checkpoint_step_id: int,
        ) -> int:
            nonlocal checkpoint_context
            replacement_frame = inspect.currentframe()
            assert replacement_frame is not None
            checkpoint_context = self.space.replay.begin_active_execution(
                source_frame=source_frame,
                replacement_frame=replacement_frame,
                replacement_target=checkpoint_replacement,
                replacement_line_numbers=(),
                parent_branch_id=parent_branch_id,
                forked_from_step_id=checkpoint_step_id,
                name="checkpoint",
            )
            replacement_line = replacement_frame.f_lineno + 1
            self.space.replay.record_active_replacement_state(
                frame=replacement_frame,
                line_number=replacement_line,
                replacement_line_numbers={replacement_line},
            )
            return value + 20

        def first_replacement(
            source_frame,
            value: int,
            parent_branch_id: int,
            checkpoint_step_id: int,
        ) -> int:
            nonlocal first_context
            replacement_frame = inspect.currentframe()
            assert replacement_frame is not None
            first_context = self.space.replay.begin_active_execution(
                source_frame=source_frame,
                replacement_frame=replacement_frame,
                replacement_target=first_replacement,
                replacement_line_numbers=(),
                name="first edit",
            )
            replacement_line = replacement_frame.f_lineno + 1
            self.space.replay.record_active_replacement_state(
                frame=replacement_frame,
                line_number=replacement_line,
                replacement_line_numbers={replacement_line},
            )
            value += 10
            return checkpoint_replacement(
                replacement_frame,
                value,
                parent_branch_id,
                checkpoint_step_id,
            )

        def original(value: int) -> int:
            nonlocal historical_step_id
            value += 1
            source_frame = inspect.currentframe()
            assert source_frame is not None
            monitor = SpaceTimeMonitor.get_instance()
            assert monitor is not None
            monitor.flush()
            branch = monitor.current_branch
            assert branch is not None and branch.id is not None
            historical_step = branch.steps[0]
            assert historical_step.id is not None
            historical_step_id = historical_step.id
            return first_replacement(
                source_frame,
                value,
                branch.id,
                historical_step.id,
            )

        original = self.space.capture.line(original)
        with self.space.capture.recording(mode="line") as recording:
            self.assertEqual(original(2), 33)
            final_branch = self.space.replay.finish()

        assert first_context is not None
        assert checkpoint_context is not None
        assert historical_step_id is not None
        first_branch = self.space.data.get_branch(first_context.branch_id)
        self.assertEqual(first_branch.status, "completed")
        self.assertEqual(final_branch.parent_branch_id, recording.branch_id)
        self.assertEqual(final_branch.forked_from_step_id, historical_step_id)
        self.assertEqual(checkpoint_context.forked_from_step.id, historical_step_id)
        self.assertEqual(
            final_branch.steps[0].source_step_id,
            historical_step_id,
        )

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

    def test_default_replay_policy_does_not_use_alignment(self) -> None:
        _, root, source, _ = self.record_external_step()

        @self.space.capture.function
        def changed(value: int) -> int:
            return value + 20

        result = self.space.replay.run(
            lambda context: changed(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
        )

        self.assertFalse(ReplayAlignmentPolicy().enabled)
        self.assertIsNone(result.alignment)

    def test_online_alignment_selects_external_interactions_for_each_step(
        self,
    ) -> None:
        @self.space.capture.external
        def read_external(value: int) -> int:
            return value * 10

        @self.space.capture.function
        def original(value: int) -> int:
            return read_external(value)

        with self.space.capture.recording(name="root") as recording:
            original(1)
            original(2)
        root = self.space.data.get_branch(recording.branch_id)

        @self.space.capture.function
        def changed(mocked_external) -> int:
            return mocked_external(999)

        def execute(context):
            mocked = context.external.mock(read_external)
            return changed(mocked), changed(mocked)

        result = self.space.replay.run(
            execute,
            parent_branch_id=root.id,
            forked_from_step_id=root.steps[0].id,
            alignment_policy=ReplayAlignmentPolicy(
                algorithm=_ReplayAlignment(),
            ),
            require_external_consumption=True,
        )

        self.assertEqual(result.value, (10, 20))
        self.assertIsNotNone(result.alignment)
        self.assertEqual(len(result.alignment.links), 2)
        self.assertEqual(
            result.alignment.reference_for(result.branch.steps[1]),
            root.steps[1],
        )

    def test_online_alignment_resumes_an_outer_steps_external_script(
        self,
    ) -> None:
        @self.space.capture.external
        def read_external(value: int) -> int:
            return value * 10

        @self.space.capture.function
        def original_inner() -> int:
            return read_external(2)

        @self.space.capture.function
        def original_outer() -> tuple[int, int, int]:
            before = read_external(1)
            nested = original_inner()
            after = read_external(3)
            return before, nested, after

        with self.space.capture.recording() as recording:
            original_outer()
        root = self.space.data.get_branch(recording.branch_id)

        @self.space.capture.function
        def changed_inner(mocked_external) -> int:
            return mocked_external(200)

        @self.space.capture.function
        def changed_outer(mocked_external) -> tuple[int, int, int]:
            before = mocked_external(100)
            nested = changed_inner(mocked_external)
            after = mocked_external(300)
            return before, nested, after

        def execute(context):
            return changed_outer(context.external.mock(read_external))

        result = self.space.replay.run(
            execute,
            parent_branch_id=root.id,
            forked_from_step_id=root.steps[0].id,
            alignment_policy=ReplayAlignmentPolicy(
                algorithm=_ReplayAlignment(),
            ),
            require_external_consumption=True,
        )

        self.assertEqual(result.value, (10, 20, 30))
        self.assertEqual(len(result.alignment.links), 2)

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
