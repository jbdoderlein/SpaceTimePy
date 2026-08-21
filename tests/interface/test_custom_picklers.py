from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spacetimepy import SpaceTime
from spacetimepy.core.model import Base
from tests.serialization_support import CustomValue, CustomValuePickler
from tests.support import SpaceTimeTestCase


class TestCustomPicklers(SpaceTimeTestCase):
    def test_open_uses_custom_pickler_for_capture_and_data_access(self) -> None:
        self.space.close()
        self.space = SpaceTime.open(custom_picklers=[CustomValuePickler])

        @self.space.capture.function
        def echo(value: CustomValue) -> CustomValue:
            return value

        with self.space.capture.recording() as recording:
            echo(CustomValue("captured"))

        call = self.space.data.get_branch(recording.branch_id).steps[0].function_call
        entry_value = self.space.data.load_references(
            call.entry_local_references
        )["value"]
        returned_value = self.space.data.load_value(call.return_reference)
        self.assertEqual(entry_value, CustomValue("captured"))
        self.assertEqual(returned_value, CustomValue("captured"))
        self.assertNotIn("capture_errors", call.attributes)

        replay_context = self.space.replay.prepare(
            parent_branch_id=recording.branch_id,
            forked_from_step_id=self.space.data.get_branch(recording.branch_id).steps[0].id,
        )
        self.assertEqual(replay_context.locals["value"], CustomValue("captured"))

    def test_dill_captures_value_without_custom_pickler(self) -> None:
        @self.space.capture.function
        def inspect_value(value: CustomValue, number: int) -> int:
            return number + 1

        with self.space.capture.recording() as recording:
            inspect_value(CustomValue("captured by Dill"), 4)

        call = self.space.data.get_branch(recording.branch_id).steps[0].function_call
        self.assertEqual(
            self.space.data.load_references(call.entry_local_references),
            {
                "value": CustomValue("captured by Dill"),
                "number": 4,
            },
        )
        self.assertNotIn("capture_errors", call.attributes)

    def test_from_session_accepts_same_custom_pickler_configuration(self) -> None:
        self.space.close()
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        database = Session(engine)
        runtime = SpaceTime.from_session(
            database,
            custom_picklers=[CustomValuePickler],
        )
        try:
            self.assertIn(
                CustomValue,
                runtime._serializer.dispatch_table,
            )
        finally:
            runtime.close()
            database.close()
            engine.dispose()
