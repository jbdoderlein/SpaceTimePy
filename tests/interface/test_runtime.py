from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import spacetimepy
from spacetimepy import SpaceTime, open_spacetime
from spacetimepy.core.model import Base
from tests.support import SpaceTimeTestCase


class TestSpaceTimeRuntime(SpaceTimeTestCase):
    def test_runtime_composes_the_three_public_services(self) -> None:
        self.assertIs(self.space.capture._database, self.space.data._database)
        self.assertIs(self.space.replay._data, self.space.data)
        self.assertFalse(self.space.is_closed)

    def test_open_spacetime_is_the_public_factory(self) -> None:
        self.space.close()
        replacement = open_spacetime()
        try:
            self.assertIsInstance(replacement, SpaceTime)
        finally:
            replacement.close()

    def test_close_is_idempotent(self) -> None:
        self.space.close()
        self.space.close()
        self.assertTrue(self.space.is_closed)

    def test_runtime_cannot_close_an_active_recording(self) -> None:
        self.space.capture.begin_recording()
        with self.assertRaisesRegex(RuntimeError, "recording or replay is active"):
            self.space.close()
        self.space.capture.finish_recording("cancelled")
        self.space.close()

    def test_from_session_does_not_close_application_owned_session(self) -> None:
        self.space.close()
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        database = Session(engine)
        runtime = SpaceTime.from_session(database)
        try:
            runtime.close()
            self.assertEqual(database.scalar(select(1)), 1)
        finally:
            database.close()
            engine.dispose()

    def test_file_database_can_be_reopened_through_public_dtos(self) -> None:
        self.space.close()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "trace.db"
            with SpaceTime.open(database_path) as writer:
                @writer.capture.function
                def calculate(value: int) -> int:
                    return value + 1

                with writer.capture.recording(name="persisted") as recording:
                    calculate(3)

            with SpaceTime.open(database_path, create_schema=False) as reader:
                session = reader.data.get_session(recording.session_id)
                branch = reader.data.get_branch(recording.branch_id)
                self.assertEqual(session.name, "persisted")
                self.assertEqual(branch.steps[0].function_call.function_name, "calculate")

    def test_runtime_context_rolls_back_uncommitted_recording_on_error(self) -> None:
        self.space.close()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "rollback.db"

            with (
                self.assertRaisesRegex(RuntimeError, "application failure"),
                SpaceTime.open(database_path) as writer,
            ):
                @writer.capture.function
                def calculate() -> int:
                    return 1

                with writer.capture.recording(commit=False):
                    calculate()
                raise RuntimeError("application failure")

            with SpaceTime.open(database_path, create_schema=False) as reader:
                self.assertEqual(reader.data.list_sessions(), ())

    def test_top_level_package_exports_v2_interface_not_core_models(self) -> None:
        self.assertEqual(spacetimepy.__version__, "2.0.0")
        self.assertIn("SpaceTime", spacetimepy.__all__)
        self.assertIn("line", spacetimepy.__all__)
        self.assertNotIn("FunctionCall", spacetimepy.__all__)
        self.assertFalse(hasattr(spacetimepy, "ObjectManager"))
