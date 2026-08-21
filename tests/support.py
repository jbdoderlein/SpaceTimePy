"""Shared database and singleton lifecycle helpers for the v2 suite."""

from __future__ import annotations

import unittest
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from spacetimepy import SpaceTime, clear_capture_declarations
from spacetimepy.core.model import Base, ExecutionBranch, ExecutionSession, StepKind
from spacetimepy.core.monitoring import SpaceTimeMonitor


class DatabaseTestCase(unittest.TestCase):
    """Provide an isolated SQLAlchemy session for core-level tests."""

    engine: Engine
    database: Session

    def setUp(self) -> None:
        clear_capture_declarations()
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.database = Session(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        monitor = SpaceTimeMonitor.get_instance()
        if monitor is not None and monitor.database is self.database:
            monitor.shutdown(commit=False)
        self.database.rollback()
        self.database.close()
        self.engine.dispose()

    def create_monitor(
        self,
        *,
        flush_batch_size: int = 256,
        profile_capture: bool = False,
    ) -> SpaceTimeMonitor:
        return SpaceTimeMonitor(
            self.database,
            flush_batch_size=flush_batch_size,
            profile_capture=profile_capture,
        )

    def start_branch(
        self,
        monitor: SpaceTimeMonitor,
        *,
        kind: StepKind = StepKind.FUNCTION_CALL,
        name: str = "test",
    ) -> ExecutionBranch:
        execution_session = ExecutionSession(name=name, step_kind=kind)
        branch = ExecutionBranch(session=execution_session, name="main")
        monitor.start_branch(branch)
        return branch


class SpaceTimeTestCase(unittest.TestCase):
    """Provide a fresh public runtime and reliably release its singleton."""

    space: SpaceTime

    def setUp(self) -> None:
        clear_capture_declarations()
        self.space = SpaceTime.open()

    def tearDown(self) -> None:
        if self.space.is_closed:
            clear_capture_declarations()
            return

        monitor = SpaceTimeMonitor.get_instance()
        if monitor is not None and monitor.current_branch is not None:
            if self.space.replay._active_context is not None:
                self.space.replay.finish("cancelled", commit=False)
            else:
                self.space.capture.finish_recording("cancelled", commit=False)
        clear_capture_declarations()
        self.space.close(commit=False)


def assert_dto(test_case: unittest.TestCase, value: Any) -> None:
    """Assert that a public result is not a live SQLAlchemy model."""

    test_case.assertFalse(hasattr(value, "_sa_instance_state"))
