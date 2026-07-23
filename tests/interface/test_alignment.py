from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

from spacetimepy import (
    STACK_SNAPSHOT_ALIGNMENT,
    AlignmentLink,
    AlignmentRelation,
    AlignmentValidationError,
    BranchDTO,
    CodeDefinitionDTO,
    CodeDiffLineMapper,
    OfflineAlignmentContext,
    OnlineAlignmentContext,
    ReplayAlignmentPolicy,
    StepDTO,
)
from tests.support import SpaceTimeTestCase

if TYPE_CHECKING:
    from spacetimepy import OnlineAlignmentSession


class _OfflineProbe:
    name = "probe"
    version = "1"

    def __init__(self, contexts: list[OfflineAlignmentContext]) -> None:
        self._contexts = contexts

    def align(
        self,
        context: OfflineAlignmentContext,
    ) -> tuple[AlignmentLink, ...]:
        self._contexts.append(context)
        return (
            AlignmentLink(
                context.reference_steps[0],
                context.target_steps[0],
                AlignmentRelation.UPDATED,
            ),
            *(
                AlignmentLink(step, None, AlignmentRelation.DELETED)
                for step in context.reference_steps[1:]
            ),
        )


class _OnlineProbeSession:
    def __init__(self, context: OnlineAlignmentContext) -> None:
        self.context = context
        self._aligned = 0

    def align(self, target_step: StepDTO) -> tuple[AlignmentLink, ...]:
        reference_step = self.context.reference_steps[self._aligned]
        self._aligned += 1
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
            for step in self.context.reference_steps[self._aligned :]
        )


class _OnlineProbe:
    name = "online-probe"
    version = "1"

    def __init__(self, contexts: list[OnlineAlignmentContext]) -> None:
        self._contexts = contexts

    def start(self, context: OnlineAlignmentContext) -> OnlineAlignmentSession:
        self._contexts.append(context)
        return _OnlineProbeSession(context)


class _DiffProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[CodeDefinitionDTO, CodeDefinitionDTO]] = []

    def compare(
        self,
        reference: CodeDefinitionDTO,
        target: CodeDefinitionDTO,
    ) -> object:
        self.calls.append((reference, target))
        return {"reference": reference.name, "target": target.name}


class TestAlignmentInterface(SpaceTimeTestCase):
    def create_fork(
        self,
        *,
        fork_position: int = 0,
    ) -> tuple[BranchDTO, BranchDTO]:
        @self.space.capture.function
        def calculate(value: int) -> int:
            return value + 1

        with self.space.capture.recording(name="root") as recording:
            calculate(1)
            calculate(2)
        root = self.space.data.get_branch(recording.branch_id)

        @self.space.capture.function
        def changed(value: int) -> int:
            return value + 10

        replay = self.space.replay.run(
            lambda context: changed(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=root.steps[fork_position].id,
        )
        return root, replay.branch

    def test_offline_algorithm_receives_existing_public_dtos(self) -> None:
        root, target = self.create_fork()
        contexts: list[OfflineAlignmentContext] = []

        result = self.space.alignment.compare(
            reference_branch_id=root.id,
            target_branch_id=target.id,
            algorithm=_OfflineProbe(contexts),
            options={"comparison": "semantic"},
        )

        self.assertIsNotNone(result)
        context = contexts[0]
        self.assertIsInstance(context.reference_branch, BranchDTO)
        self.assertIsInstance(context.target_branch, BranchDTO)
        self.assertTrue(
            all(isinstance(step, StepDTO) for step in context.reference_steps)
        )
        self.assertEqual(
            [step.id for step in context.reference_steps],
            [step.id for step in root.steps],
        )
        self.assertEqual(context.target_steps, target.steps)
        self.assertEqual(context.options, {"comparison": "semantic"})
        with self.assertRaises(TypeError):
            context.options["comparison"] = "changed"
        self.assertEqual(
            [link.relation for link in result.links],
            [AlignmentRelation.UPDATED, AlignmentRelation.DELETED],
        )
        self.assertEqual(
            result.link_for_target(target.steps[0]),
            result.links[0],
        )
        self.assertEqual(
            result.link_for_reference(root.steps[1].id),
            result.links[1],
        )
        self.assertEqual(result.reference_for(target.steps[0]), root.steps[0])
        self.assertIsNone(result.target_for(root.steps[1]))

    def test_reference_steps_start_at_the_fork_step(self) -> None:
        root, target = self.create_fork(fork_position=1)
        contexts: list[OfflineAlignmentContext] = []

        self.space.alignment.compare(
            reference_branch_id=root.id,
            target_branch_id=target.id,
            algorithm=_OfflineProbe(contexts),
        )

        self.assertEqual(contexts[0].reference_steps, (root.steps[1],))

    def test_named_algorithms_are_runtime_local_factories(self) -> None:
        root, target = self.create_fork()
        contexts: list[OfflineAlignmentContext] = []
        self.space.alignment.register(
            "probe",
            version="1",
            offline=lambda: _OfflineProbe(contexts),
        )

        result = self.space.alignment.compare(
            reference_branch_id=root.id,
            target_branch_id=target.id,
            algorithm="probe",
        )

        self.assertEqual(result.algorithm, "probe")
        self.assertEqual(result.algorithm_version, "1")
        self.assertEqual(len(contexts), 1)
        descriptor = next(
            item
            for item in self.space.alignment.registry.algorithms()
            if item.name == "probe"
        )
        self.assertTrue(descriptor.offline)
        self.assertFalse(descriptor.online)

    def test_no_algorithm_means_no_alignment(self) -> None:
        root, target = self.create_fork()

        self.assertIsNone(
            self.space.alignment.compare(
                reference_branch_id=root.id,
                target_branch_id=target.id,
            )
        )

    def test_stack_snapshot_alignment_is_the_default_for_line_traces(self) -> None:
        @self.space.capture.line
        def calculate(value: int) -> int:
            value += 1
            value *= 2
            return value

        with self.space.capture.recording(mode="line") as recording:
            calculate(2)
        root = self.space.data.get_branch(recording.branch_id)

        @self.space.capture.line
        def changed(value: int) -> int:
            value += 1
            value += 10
            value *= 3
            return value

        replay = self.space.replay.run(
            lambda context: changed(context.locals["value"]),
            parent_branch_id=root.id,
            forked_from_step_id=root.steps[0].id,
        )

        result = self.space.alignment.compare(
            reference_branch_id=root.id,
            target_branch_id=replay.branch.id,
        )

        self.assertEqual(result.algorithm, STACK_SNAPSHOT_ALIGNMENT)
        self.assertEqual(
            [link.relation for link in result.links],
            [
                AlignmentRelation.MATCH,
                AlignmentRelation.INSERTED,
                AlignmentRelation.UPDATED,
                AlignmentRelation.MATCH,
            ],
        )
        self.assertTrue(self.space.alignment.data.has_diff_provider("code-diff"))

        reference_code = self.space.alignment.data.code(root.steps[0])
        target_code = self.space.alignment.data.code(replay.branch.steps[0])
        line_mapping = CodeDiffLineMapper().compare(reference_code, target_code)
        self.assertTrue(line_mapping.edit_script)
        self.assertIn(
            replay.branch.steps[1].stack_snapshot.line_number,
            line_mapping.inserted_lines,
        )

    def test_alignment_data_loads_code_values_and_lazily_caches_diff(self) -> None:
        root, target = self.create_fork()
        reference_step = root.steps[0]
        target_step = target.steps[0]
        provider = _DiffProbe()
        self.space.alignment.register_diff_provider("code-diff", provider)

        reference_code = self.space.alignment.data.code(reference_step)
        target_code = self.space.alignment.data.code(target_step)
        reference_values = self.space.alignment.data.values(reference_step)
        first_diff = self.space.alignment.data.diff(reference_step, target_step)
        second_diff = self.space.alignment.data.diff(reference_step, target_step)

        self.assertEqual(reference_code.name, "calculate")
        self.assertEqual(target_code.name, "changed")
        self.assertEqual(reference_values[0]["value"], 1)
        self.assertEqual(first_diff, {"reference": "calculate", "target": "changed"})
        self.assertIs(first_diff, second_diff)
        self.assertEqual(len(provider.calls), 1)

    def test_online_algorithm_receives_target_steps_incrementally(self) -> None:
        root, target = self.create_fork()
        contexts: list[OnlineAlignmentContext] = []

        session = self.space.alignment.start_online(
            reference_branch_id=root.id,
            target_branch_id=target.id,
            algorithm=_OnlineProbe(contexts),
            options={"window": 3},
        )
        links = session.align(target.steps[0])
        remaining = session.finish()

        self.assertEqual(contexts[0].target_branch_id, target.id)
        self.assertEqual(contexts[0].options, {"window": 3})
        self.assertEqual(links[0].relation, AlignmentRelation.MATCH)
        self.assertEqual(links[0].target_step, target.steps[0])
        self.assertEqual(
            [link.relation for link in remaining],
            [AlignmentRelation.DELETED],
        )
        self.assertEqual(session.result.reference_for(target.steps[0]), root.steps[0])

    def test_online_service_rejects_a_step_from_another_branch(self) -> None:
        root, target = self.create_fork()
        session = self.space.alignment.start_online(
            reference_branch_id=root.id,
            target_branch_id=target.id,
            algorithm=_OnlineProbe([]),
        )

        with self.assertRaisesRegex(
            AlignmentValidationError,
            "not online target branch",
        ):
            session.align(root.steps[0])

    def test_link_relations_define_their_step_shapes(self) -> None:
        root, target = self.create_fork()

        valid = (
            AlignmentLink(root.steps[0], target.steps[0], "match"),
            AlignmentLink(root.steps[0], target.steps[0], "updated"),
            AlignmentLink(None, target.steps[0], "inserted"),
            AlignmentLink(root.steps[0], None, "deleted"),
        )

        self.assertEqual(
            tuple(link.relation for link in valid),
            tuple(AlignmentRelation),
        )
        with self.assertRaises(ValueError):
            AlignmentLink(None, target.steps[0], AlignmentRelation.MATCH)
        with self.assertRaises(FrozenInstanceError):
            valid[0].relation = AlignmentRelation.UPDATED

    def test_replay_alignment_policy_is_an_empty_extension_point(self) -> None:
        policy = ReplayAlignmentPolicy()

        self.assertEqual(policy, ReplayAlignmentPolicy())
        self.assertFalse(hasattr(policy, "__dict__"))
