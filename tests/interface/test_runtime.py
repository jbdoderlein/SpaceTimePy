from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import spacetimepy
from spacetimepy import SpaceTime, get_active_spacetime, open_spacetime
from spacetimepy.core.model import Base, FunctionCallCapturePerformance
from tests.support import SpaceTimeTestCase


class TestSpaceTimeRuntime(SpaceTimeTestCase):
    def test_runtime_composes_the_four_public_services(self) -> None:
        self.assertIs(self.space.capture._database, self.space.data._database)
        self.assertIs(self.space.alignment.data.trace, self.space.data)
        self.assertIs(self.space.replay._data, self.space.data)
        self.assertFalse(self.space.is_closed)
        self.assertIs(get_active_spacetime(), self.space)

    def test_closed_runtime_is_no_longer_active(self) -> None:
        self.space.close()
        self.assertIsNone(get_active_spacetime())

    def test_open_spacetime_is_the_public_factory(self) -> None:
        self.space.close()
        replacement = open_spacetime()
        try:
            self.assertIsInstance(replacement, SpaceTime)
        finally:
            replacement.close()

    def test_runtime_logging_level_is_scoped_to_the_open_runtime(self) -> None:
        package_logger = logging.getLogger("spacetimepy")
        previous_level = package_logger.level
        self.space.close()

        runtime = SpaceTime.open(logging_level="debug")
        try:
            self.assertEqual(package_logger.level, logging.DEBUG)
        finally:
            runtime.close()

        self.assertEqual(package_logger.level, previous_level)

    def test_runtime_rejects_an_unknown_logging_level(self) -> None:
        self.space.close()

        with self.assertRaisesRegex(ValueError, "Unknown logging level"):
            SpaceTime.open(logging_level="verbose")

    def test_runtime_capture_profile_is_persisted_in_the_trace(self) -> None:
        self.space.close()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "performance.db"
            with SpaceTime.open(database_path, profile_capture=True) as runtime:
                @runtime.capture.function
                def calculate(value: int) -> int:
                    return value + 1

                with runtime.capture.recording():
                    calculate(2)

            engine = create_engine(f"sqlite+pysqlite:///{database_path}")
            try:
                with Session(engine) as database:
                    performance = database.scalars(
                        select(FunctionCallCapturePerformance)
                    ).one()
                    self.assertGreaterEqual(performance.direct_capture_ns, 0)
                    self.assertGreaterEqual(
                        performance.inclusive_capture_ns,
                        performance.direct_capture_ns,
                    )
            finally:
                engine.dispose()

    def test_runtime_logs_recoverable_capture_failures(self) -> None:
        self.space.close()
        runtime = SpaceTime.open(logging_level="WARNING")

        class Unserializable:
            def __reduce__(self):
                raise TypeError("no reducer is available")

        @runtime.capture.function
        def echo(value: Unserializable) -> Unserializable:
            return value

        try:
            with (
                self.assertLogs(
                    "spacetimepy.core.monitoring",
                    level="WARNING",
                ) as captured,
                runtime.capture.recording() as recording,
            ):
                echo(Unserializable())

            messages = "\n".join(captured.output)
            self.assertIn("could not capture variable 'value'", messages)
            self.assertIn("could not capture return value", messages)
            call = runtime.data.get_branch(recording.branch_id).steps[0].function_call
            self.assertIn("value", call.attributes["capture_errors"])
            self.assertIn("return", call.attributes["capture_errors"])
        finally:
            runtime.close()

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

    def test_reopened_session_lists_a_new_replay_branch(self) -> None:
        self.space.close()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "trace.db"
            with SpaceTime.open(database_path) as writer:
                @writer.capture.function
                def calculate(value: int) -> int:
                    return value + 1

                with writer.capture.recording() as recording:
                    calculate(3)
                source = writer.data.get_branch(recording.branch_id).steps[0]

            with SpaceTime.open(database_path, create_schema=False) as reader:
                reader.capture.function(calculate)
                result = reader.replay.run(
                    lambda context: calculate(context.locals["value"]),
                    parent_branch_id=recording.branch_id,
                    forked_from_step_id=source.id,
                )

                session = reader.data.get_session(recording.session_id)
                self.assertEqual(
                    [branch.id for branch in session.branches],
                    [recording.branch_id, result.branch.id],
                )

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
