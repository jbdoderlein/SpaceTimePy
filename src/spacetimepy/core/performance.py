"""Opt-in measurement of SpaceTimePy capture overhead."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import FunctionCallCapturePerformance

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

    from .model import FunctionCall


@dataclass(slots=True)
class CallCaptureProfile:
    """Transient accumulator kept outside the measured capture regions."""

    function_call: FunctionCall
    parent: CallCaptureProfile | None
    start_capture_ns: int
    return_capture_ns: int = 0
    unwind_capture_ns: int = 0
    line_capture_ns: int = 0
    child_capture_ns: int = 0
    line_event_count: int = 0
    line_snapshot_count: int = 0
    filtered_line_event_count: int = 0
    line_capture_min_ns: int | None = None
    line_capture_max_ns: int | None = None
    completed: bool = False

    @property
    def direct_capture_ns(self) -> int:
        return (
            self.start_capture_ns
            + self.return_capture_ns
            + self.unwind_capture_ns
            + self.line_capture_ns
        )

    @property
    def inclusive_capture_ns(self) -> int:
        return self.direct_capture_ns + self.child_capture_ns


class CaptureProfiler:
    """Measure callback work and persist completed per-call aggregates.

    Timing ends before any method mutates an accumulator. Completed metrics
    remain in memory until branch finalization, so their ORM construction and
    database persistence cannot contribute to a later callback measurement.
    """

    def __init__(self, clock: Callable[[], int] = time.perf_counter_ns) -> None:
        self._clock = clock
        self._completed: list[CallCaptureProfile] = []

    def begin(self) -> int:
        """Start one measured capture region."""

        return self._clock()

    def finish(self, started_ns: int) -> int:
        """Close a region before profiler bookkeeping begins."""

        return max(0, self._clock() - started_ns)

    def start_call(
        self,
        function_call: FunctionCall,
        *,
        parent: CallCaptureProfile | None,
        capture_ns: int,
    ) -> CallCaptureProfile:
        return CallCaptureProfile(
            function_call=function_call,
            parent=parent,
            start_capture_ns=capture_ns,
        )

    def record_line(
        self,
        profile: CallCaptureProfile,
        *,
        capture_ns: int,
        snapshot_recorded: bool,
    ) -> None:
        self._require_open(profile)
        profile.line_event_count += 1
        profile.line_capture_ns += capture_ns
        if snapshot_recorded:
            profile.line_snapshot_count += 1
        else:
            profile.filtered_line_event_count += 1
        if (
            profile.line_capture_min_ns is None
            or capture_ns < profile.line_capture_min_ns
        ):
            profile.line_capture_min_ns = capture_ns
        if (
            profile.line_capture_max_ns is None
            or capture_ns > profile.line_capture_max_ns
        ):
            profile.line_capture_max_ns = capture_ns

    def finish_call(
        self,
        profile: CallCaptureProfile,
        *,
        capture_ns: int,
        raised: bool,
    ) -> None:
        self._require_open(profile)
        if raised:
            profile.unwind_capture_ns = capture_ns
        else:
            profile.return_capture_ns = capture_ns
        profile.completed = True
        if profile.parent is not None:
            profile.parent.child_capture_ns += profile.inclusive_capture_ns
        self._completed.append(profile)

    def persist(self, database: Session) -> None:
        """Stage completed metrics after capture execution has stopped."""

        if not self._completed:
            return
        database.add_all(
            [
                FunctionCallCapturePerformance(
                    function_call=profile.function_call,
                    start_capture_ns=profile.start_capture_ns,
                    return_capture_ns=profile.return_capture_ns,
                    unwind_capture_ns=profile.unwind_capture_ns,
                    line_capture_ns=profile.line_capture_ns,
                    direct_capture_ns=profile.direct_capture_ns,
                    inclusive_capture_ns=profile.inclusive_capture_ns,
                    line_event_count=profile.line_event_count,
                    line_snapshot_count=profile.line_snapshot_count,
                    filtered_line_event_count=profile.filtered_line_event_count,
                    line_capture_min_ns=profile.line_capture_min_ns,
                    line_capture_max_ns=profile.line_capture_max_ns,
                )
                for profile in self._completed
            ]
        )
        self._completed.clear()

    def discard(self) -> None:
        """Forget measurements belonging to a rolled-back recording."""

        self._completed.clear()

    @staticmethod
    def _require_open(profile: CallCaptureProfile) -> None:
        if profile.completed:
            raise RuntimeError("Capture profile is already complete")


__all__ = ["CallCaptureProfile", "CaptureProfiler"]
