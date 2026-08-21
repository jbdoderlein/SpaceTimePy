"""Composition root for the public SpaceTimePy programmatic interface."""

from __future__ import annotations

import logging
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
_PACKAGE_LOGGER_NAME = "spacetimepy"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _resolve_logging_level(level: int | str | None) -> int | None:
    if level is None:
        return None
    if isinstance(level, bool):
        raise TypeError("logging_level must be an integer, level name, or None")
    if isinstance(level, int):
        return level
    if not isinstance(level, str):
        raise TypeError("logging_level must be an integer, level name, or None")

    normalized = level.strip().upper()
    resolved = logging.getLevelNamesMapping().get(normalized)
    if resolved is None:
        valid_names = ", ".join(
            name for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        )
        raise ValueError(
            f"Unknown logging level {level!r}; expected one of {valid_names}"
        )
    return resolved


def _validate_profile_capture(value: bool) -> bool:
    if not isinstance(value, bool):
        raise TypeError("profile_capture must be a boolean")
    return value


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
        logging_level: int | str | None = None,
        profile_capture: bool = False,
    ) -> None:
        global _active_runtime
        if _active_runtime is not None and not _active_runtime.is_closed:
            raise RuntimeError("A SpaceTime runtime is already active in this process")

        resolved_logging_level = _resolve_logging_level(logging_level)
        profile_capture = _validate_profile_capture(profile_capture)
        self._serializer = PickleSerializer(custom_picklers)
        monitor_options: dict[str, Any] = {
            "flush_batch_size": flush_batch_size,
            "serializer": self._serializer,
            "profile_capture": profile_capture,
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
        self._package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
        self._previous_logging_level = self._package_logger.level
        self._logging_handler: logging.Handler | None = None
        self._logging_configured = resolved_logging_level is not None
        if resolved_logging_level is not None:
            self._configure_logging(resolved_logging_level)
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
            self._restore_logging()
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
        logging_level: int | str | None = None,
        profile_capture: bool = False,
    ) -> SpaceTime:
        """Open a SQLite path/URL and create a complete SpaceTime runtime.

        ``logging_level`` accepts a standard logging name such as ``"INFO"``
        or an integer level. When supplied, SpaceTimePy diagnostics are enabled
        for this runtime and recoverable capture failures are logged.
        ``profile_capture`` stores opt-in per-call capture-overhead metrics.
        """

        resolved_logging_level = _resolve_logging_level(logging_level)
        profile_capture = _validate_profile_capture(profile_capture)
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
                logging_level=resolved_logging_level,
                profile_capture=profile_capture,
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
        logging_level: int | str | None = None,
        profile_capture: bool = False,
    ) -> SpaceTime:
        """Build the public interfaces around an application-owned session."""

        resolved_logging_level = _resolve_logging_level(logging_level)
        profile_capture = _validate_profile_capture(profile_capture)
        if create_schema:
            bind = database.get_bind()
            Base.metadata.create_all(bind)
        return cls(
            database,
            owns_database=False,
            flush_batch_size=flush_batch_size,
            tool_id=tool_id,
            custom_picklers=custom_picklers,
            logging_level=resolved_logging_level,
            profile_capture=profile_capture,
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
        self._restore_logging()

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

    def _configure_logging(self, level: int) -> None:
        self._package_logger.setLevel(level)
        if self._package_logger.hasHandlers():
            return

        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        self._package_logger.addHandler(handler)
        self._logging_handler = handler

    def _restore_logging(self) -> None:
        if not self._logging_configured:
            return
        handler = self._logging_handler
        if handler is not None:
            self._package_logger.removeHandler(handler)
            handler.close()
            self._logging_handler = None
        self._package_logger.setLevel(self._previous_logging_level)
        self._logging_configured = False


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
