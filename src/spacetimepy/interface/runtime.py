"""Composition root for the public SpaceTimePy programmatic interface."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spacetimepy.core.model import Base
from spacetimepy.core.monitoring import SpaceTimeMonitor
from spacetimepy.core.serialization import CustomPickler, PickleSerializer
from spacetimepy.interface.alignment import AlignmentService
from spacetimepy.interface.capture import CaptureInterface, capture_registry
from spacetimepy.interface.data import TraceData
from spacetimepy.interface.replay import ReplayInterface
from spacetimepy.interface.standard_external_interactions import (
    StandardExternalInteractionRegistry,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType

    from sqlalchemy import Engine


_active_runtime: SpaceTime | None = None


class SpaceTime:
    """Own one database unit of work and the four public interfaces.

    Use :meth:`open` for the usual SQLite-backed runtime or
    :meth:`from_session` when an application already controls SQLAlchemy.
    ``SpaceTimeMonitor`` remains a process singleton because ``sys.monitoring``
    is itself VM-global.
    """

    def __init__(
        self,
        database: Session,
        *,
        engine: Engine | None = None,
        owns_database: bool = False,
        flush_batch_size: int = 256,
        tool_id: int | None = None,
        custom_picklers: Iterable[CustomPickler] = (),
    ) -> None:
        global _active_runtime
        if _active_runtime is not None and not _active_runtime.is_closed:
            raise RuntimeError("A SpaceTime runtime is already active in this process")

        self._serializer = PickleSerializer(custom_picklers)
        monitor_options: dict[str, Any] = {
            "flush_batch_size": flush_batch_size,
            "serializer": self._serializer,
        }
        if tool_id is not None:
            monitor_options["tool_id"] = tool_id

        self._database = database
        self._engine = engine
        self._owns_database = owns_database
        self._closed = False

        self._monitor = SpaceTimeMonitor(database, **monitor_options)
        self._standard_external_interactions = StandardExternalInteractionRegistry(
            self._monitor
        )
        try:
            self._standard_external_interactions.start()
            self.capture = CaptureInterface(
                database,
                self._monitor,
                self._standard_external_interactions,
            )
            self.data = TraceData(database, self._serializer)
            self.alignment = AlignmentService(self.data)
            self.replay = ReplayInterface(
                database,
                self._monitor,
                self.data,
                self.alignment,
            )
            capture_registry.bind(self._monitor)
            _active_runtime = self
        except BaseException:
            self._standard_external_interactions.stop()
            self._monitor.shutdown(commit=False)
            raise

    @classmethod
    def open(
        cls,
        database: str | Path = ":memory:",
        *,
        create_schema: bool = True,
        echo: bool = False,
        flush_batch_size: int = 256,
        tool_id: int | None = None,
        custom_picklers: Iterable[CustomPickler] = (),
    ) -> SpaceTime:
        """Open a SQLite path/URL and create a complete SpaceTime runtime."""

        url = cls._database_url(database)
        engine_options: dict[str, Any] = {"echo": echo}
        if url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **engine_options)
        if create_schema:
            Base.metadata.create_all(engine)
        orm_session = Session(engine, expire_on_commit=False)
        try:
            return cls(
                orm_session,
                engine=engine,
                owns_database=True,
                flush_batch_size=flush_batch_size,
                tool_id=tool_id,
                custom_picklers=custom_picklers,
            )
        except BaseException:
            orm_session.close()
            engine.dispose()
            raise

    @classmethod
    def from_session(
        cls,
        database: Session,
        *,
        create_schema: bool = False,
        flush_batch_size: int = 256,
        tool_id: int | None = None,
        custom_picklers: Iterable[CustomPickler] = (),
    ) -> SpaceTime:
        """Build the public interfaces around an application-owned session."""

        if create_schema:
            bind = database.get_bind()
            Base.metadata.create_all(bind)
        return cls(
            database,
            owns_database=False,
            flush_batch_size=flush_batch_size,
            tool_id=tool_id,
            custom_picklers=custom_picklers,
        )

    @property
    def is_closed(self) -> bool:
        return self._closed

    def commit(self) -> None:
        self._ensure_open()
        self._monitor.commit()

    def rollback(self) -> None:
        self._ensure_open()
        self._monitor.rollback()

    def close(self, *, commit: bool = True) -> None:
        """Detach monitoring and optionally close the owned ORM resources."""

        global _active_runtime
        if self._closed:
            return
        if self._monitor.current_branch is not None:
            raise RuntimeError(
                "Cannot close SpaceTime while a recording or replay is active"
            )

        capture_registry.unbind(self._monitor)
        self._standard_external_interactions.stop()
        self._monitor.shutdown(commit=False)
        if commit:
            self._database.commit()
        else:
            self._database.rollback()
        if self._owns_database:
            self._database.close()
            if self._engine is not None:
                self._engine.dispose()
        self._closed = True
        if _active_runtime is self:
            _active_runtime = None

    def __enter__(self) -> SpaceTime:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception, traceback
        self.close(commit=exception_type is None)

    @staticmethod
    def _database_url(database: str | Path) -> str:
        if isinstance(database, Path):
            return f"sqlite+pysqlite:///{database.expanduser().resolve()}"
        if database == ":memory:":
            return "sqlite+pysqlite:///:memory:"
        if "://" in database:
            return database
        return f"sqlite+pysqlite:///{Path(database).expanduser().resolve()}"

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("This SpaceTime runtime is closed")


def open_spacetime(
    database: str | Path = ":memory:",
    **options: Any,
) -> SpaceTime:
    """Convenience function equivalent to :meth:`SpaceTime.open`."""

    return SpaceTime.open(database, **options)


def get_active_spacetime() -> SpaceTime | None:
    """Return the open process runtime used by in-process integrations."""

    runtime = _active_runtime
    return runtime if runtime is not None and not runtime.is_closed else None


__all__ = ["SpaceTime", "get_active_spacetime", "open_spacetime"]
