from __future__ import annotations

import datetime

from sqlalchemy.exc import IntegrityError

from spacetimepy.core.model import (
    CodeDefinition,
    ExecutionBranch,
    ExecutionSession,
    ExecutionStep,
    ExternalInteractionOccurrence,
    FunctionCall,
    StackSnapshot,
    StepKind,
)
from tests.support import DatabaseTestCase


def function_call(name: str) -> FunctionCall:
    return FunctionCall(
        function_name=name,
        qualified_name=name,
        started_at=datetime.datetime.now(datetime.UTC),
    )


class TestModelArchitecture(DatabaseTestCase):
    def test_vm_observations_do_not_contain_exploration_organization(self) -> None:
        forbidden = {
            "session_id",
            "branch_id",
            "position",
            "source_step_id",
            "forked_from_step_id",
        }

        self.assertTrue(forbidden.isdisjoint(FunctionCall.__table__.columns.keys()))
        self.assertTrue(forbidden.isdisjoint(StackSnapshot.__table__.columns.keys()))

    def test_session_branch_step_and_external_order_round_trip(self) -> None:
        execution_session = ExecutionSession(
            name="game",
            step_kind=StepKind.FUNCTION_CALL,
        )
        root = ExecutionBranch(session=execution_session, name="main")
        step_call = function_call("tick")
        step = ExecutionStep(
            branch=root,
            position=0,
            kind=StepKind.FUNCTION_CALL,
            function_call=step_call,
        )
        first_external = function_call("read_input")
        second_external = function_call("write_output")
        step.external_interactions.extend(
            [
                ExternalInteractionOccurrence(
                    position=0,
                    function_call=first_external,
                ),
                ExternalInteractionOccurrence(
                    position=1,
                    function_call=second_external,
                ),
            ]
        )

        self.database.add(execution_session)
        self.database.flush()

        self.assertEqual(execution_session.branches, [root])
        self.assertEqual(root.steps, [step])
        self.assertEqual(
            [item.function_call.function_name for item in step.external_interactions],
            ["read_input", "write_output"],
        )

    def test_stack_snapshot_uses_call_code_as_fallback(self) -> None:
        entry_code = CodeDefinition(
            id="entry",
            name="function",
            kind="function",
            module_path="example.py",
            code_content="def function(): pass",
        )
        changed_code = CodeDefinition(
            id="changed",
            name="function",
            kind="function",
            module_path="example.py",
            code_content="def function(): return 1",
        )
        call = function_call("function")
        call.code_definition = entry_code
        inherited = StackSnapshot(
            function_call=call,
            line_number=1,
        )
        explicit = StackSnapshot(
            function_call=call,
            code_definition=changed_code,
            line_number=2,
        )

        self.assertIs(inherited.effective_code_definition, entry_code)
        self.assertIs(explicit.effective_code_definition, changed_code)

    def test_step_payload_constraint_rejects_missing_vm_observation(self) -> None:
        execution_session = ExecutionSession(step_kind=StepKind.FUNCTION_CALL)
        root = ExecutionBranch(session=execution_session)
        root.steps.append(
            ExecutionStep(position=0, kind=StepKind.FUNCTION_CALL)
        )
        self.database.add(execution_session)

        with self.assertRaises(IntegrityError):
            self.database.flush()

    def test_child_branch_requires_a_fork_step(self) -> None:
        execution_session = ExecutionSession(step_kind=StepKind.FUNCTION_CALL)
        root = ExecutionBranch(session=execution_session)
        invalid_child = ExecutionBranch(
            session=execution_session,
            parent_branch=root,
        )
        self.database.add(invalid_child)

        with self.assertRaises(IntegrityError):
            self.database.flush()

    def test_external_positions_are_unique_within_a_step(self) -> None:
        execution_session = ExecutionSession(step_kind=StepKind.FUNCTION_CALL)
        root = ExecutionBranch(session=execution_session)
        step = ExecutionStep(
            branch=root,
            position=0,
            kind=StepKind.FUNCTION_CALL,
            function_call=function_call("tick"),
        )
        step.external_interactions.extend(
            [
                ExternalInteractionOccurrence(
                    position=0,
                    function_call=function_call("first"),
                ),
                ExternalInteractionOccurrence(
                    position=0,
                    function_call=function_call("second"),
                ),
            ]
        )
        self.database.add(execution_session)

        with self.assertRaises(IntegrityError):
            self.database.flush()
