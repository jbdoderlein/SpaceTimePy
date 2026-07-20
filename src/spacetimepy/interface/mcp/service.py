"""Agent-oriented, transport-neutral exploration of a SpaceTime trace."""

from __future__ import annotations

from collections import Counter, defaultdict
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from spacetimepy.interface.data import TraceNotFoundError

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

    from spacetimepy.interface.data import (
        BranchDTO,
        CodeDefinitionDTO,
        FunctionCallDTO,
        StackSnapshotDTO,
        StepDTO,
        StoredValueSummaryDTO,
        TraceData,
    )


type ResponseFormat = Literal["concise", "detailed"]


class PaginationPayload(TypedDict):
    """Bounded-result metadata exposed consistently by all agent tools."""

    returned: int
    total: int | None
    truncated: bool
    next_cursor: str | None


class ResourceLinkPayload(TypedDict):
    """A discoverable trace resource related to a tool result."""

    uri: str
    name: str
    description: str
    mime_type: str


class AgentResult(TypedDict):
    """Stable envelope optimized for model consumption and validation."""

    summary: str
    data: dict[str, Any]
    resource_links: list[ResourceLinkPayload]
    pagination: PaginationPayload
    warnings: list[str]


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _duration(
    started_at: datetime.datetime,
    completed_at: datetime.datetime | None,
) -> float | None:
    if completed_at is None:
        return None
    return (completed_at - started_at).total_seconds()


def _bounded_preview(value: Any, *, depth: int = 0) -> Any:
    """Return compact JSON-compatible context from an explicitly trusted value."""

    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= 1_000 else value[:997] + "..."
    if isinstance(value, bytes):
        return f"<bytes: {len(value)} bytes>"
    if depth >= 2:
        return f"<{type(value).__name__}>"
    if isinstance(value, list | tuple | set | frozenset):
        selected = list(value)
        result = [_bounded_preview(item, depth=depth + 1) for item in selected[:12]]
        if len(selected) > 12:
            result.append(f"... {len(selected) - 12} more")
        return result
    if isinstance(value, dict):
        items = list(value.items())
        result = {
            str(key): _bounded_preview(item, depth=depth + 1)
            for key, item in items[:12]
        }
        if len(items) > 12:
            result["..."] = f"{len(items) - 12} more"
        return result
    try:
        text = repr(value)
    except Exception as error:  # noqa: BLE001 - a preview must not break exploration
        return f"<unrepresentable {type(value).__name__}: {type(error).__name__}>"
    return text if len(text) <= 1_000 else text[:997] + "..."


class AgentTraceService:
    """Serve bounded debugging operations through public SpaceTime DTOs only.

    Non-primitive stored values are never deserialized unless
    ``trust_stored_values`` was explicitly selected by the server operator.
    Primitive JSON values remain safe and useful in state previews.
    """

    MAX_SEARCH_LIMIT = 100
    MAX_SLICE_STEPS = 50
    MAX_STATE_NAMES = 40
    MAX_TREE_DEPTH = 4
    MAX_TREE_NODES = 100
    MAX_RESOURCE_STEPS = 200

    def __init__(
        self,
        data: TraceData,
        *,
        trust_stored_values: bool = False,
        refresh_before_read: bool = False,
    ) -> None:
        self.data = data
        self.trust_stored_values = trust_stored_values
        self.refresh_before_read = refresh_before_read
        self._lock = RLock()
        self._stored_values: dict[str, StoredValueSummaryDTO] | None = None
        self._locations: dict[int, tuple[tuple[int, int, int], ...]] | None = None

    def trace_overview(
        self,
        *,
        response_format: ResponseFormat = "concise",
    ) -> AgentResult:
        """Summarize trace shape, branch topology, and captured-code coverage."""

        self._validate_response_format(response_format)
        self._prepare_read()
        with self._lock:
            statistics = self.data.get_statistics()
            sessions = self.data.list_sessions()
            calls = self.data.list_function_calls()

            files = Counter(call.file_path for call in calls if call.file_path)
            functions = Counter(
                call.qualified_name or call.function_name for call in calls
            )
            detail_limit = 100 if response_format == "detailed" else 30
            file_items = [
                {"file_path": name, "call_count": count}
                for name, count in files.most_common(detail_limit)
            ]
            function_items = [
                {"function": name, "call_count": count}
                for name, count in functions.most_common(detail_limit)
            ]
            session_items = [self._session_summary(session) for session in sessions]

        warnings: list[str] = []
        if len(files) > detail_limit:
            warnings.append(
                f"File coverage is truncated to {detail_limit} entries; "
                "use spacetime_search_calls with a file or text filter."
            )
        if len(functions) > detail_limit:
            warnings.append(
                f"Function coverage is truncated to {detail_limit} entries; "
                "use spacetime_search_calls for targeted discovery."
            )
        if not sessions:
            warnings.append(
                "The trace contains no execution session. Use the prepare_capture "
                "prompt or read spacetime://guides/capture before recording a scenario."
            )

        return self._result(
            summary=(
                f"Trace contains {statistics.session_count} session(s), "
                f"{statistics.branch_count} branch(es), and "
                f"{statistics.step_count} recorded step(s)."
            ),
            data={
                "database": self.data.database_label,
                "read_only": True,
                "replay_available": False,
                "comparison_available": False,
                "statistics": {
                    "sessions": statistics.session_count,
                    "branches": statistics.branch_count,
                    "steps": statistics.step_count,
                    "function_calls": statistics.function_call_count,
                    "stack_snapshots": statistics.stack_snapshot_count,
                    "external_interactions": statistics.external_interaction_count,
                    "code_definitions": statistics.code_definition_count,
                    "stored_values": statistics.stored_value_count,
                },
                "sessions": session_items,
                "capture_coverage": {
                    "modes": sorted({session.step_kind for session in sessions}),
                    "files": file_items,
                    "functions": function_items,
                    "has_state": statistics.stored_value_count > 0,
                    "has_external_interactions": (
                        statistics.external_interaction_count > 0
                    ),
                },
                "capture_guidance": {
                    "resource": "spacetime://guides/capture",
                    "prompt": "prepare_capture",
                },
            },
            links=[
                self._link(
                    "spacetime://trace",
                    "Trace overview",
                    "Stable resource representation of this trace overview.",
                ),
                self._link(
                    "spacetime://guides/capture",
                    "Capture guide",
                    "How to add a targeted SpaceTimePy capture when evidence is missing.",
                    mime_type="text/markdown",
                ),
            ],
            returned=len(session_items),
            total=len(session_items),
            truncated=bool(len(files) > detail_limit or len(functions) > detail_limit),
            warnings=warnings,
        )

    def search_calls(
        self,
        *,
        query: str = "",
        session_id: int | None = None,
        branch_id: int | None = None,
        outcome: Literal["running", "returned", "raised"] | None = None,
        limit: int = 20,
        cursor: str | None = None,
        response_format: ResponseFormat = "concise",
    ) -> AgentResult:
        """Search captured VM calls without requiring an agent to crawl call IDs."""

        self._validate_response_format(response_format)
        selected_limit = self._validate_limit(limit, self.MAX_SEARCH_LIMIT)
        offset = self._cursor_offset(cursor)
        self._prepare_read()
        with self._lock:
            calls = self.data.list_function_calls()
            snapshots = self.data.list_stack_snapshots()
            locations = self._call_locations()

            snapshot_counts = Counter(item.function_call_id for item in snapshots)
            lowered_query = query.strip().casefold()

            def include(call: FunctionCallDTO) -> bool:
                call_locations = locations.get(call.id, ())
                if outcome is not None and call.outcome != outcome:
                    return False
                if session_id is not None and not any(
                    location[0] == session_id for location in call_locations
                ):
                    return False
                if branch_id is not None and not any(
                    location[1] == branch_id for location in call_locations
                ):
                    return False
                if not lowered_query:
                    return True
                haystack = " ".join(
                    part
                    for part in (
                        call.function_name,
                        call.qualified_name,
                        call.module_name,
                        call.file_path,
                        call.outcome,
                    )
                    if part
                ).casefold()
                return lowered_query in haystack

            filtered = [call for call in calls if include(call)]
            page = filtered[offset : offset + selected_limit]
            items = [
                self._call_summary(
                    call,
                    locations=locations.get(call.id, ()),
                    snapshot_count=snapshot_counts[call.id],
                    detailed=response_format == "detailed",
                )
                for call in page
            ]

        next_offset = offset + len(page)
        truncated = next_offset < len(filtered)
        warnings = []
        if not filtered:
            warnings.append(
                "No captured call matches these filters. This means the call is "
                "absent from the recorded evidence, not necessarily that it never ran."
            )
        links = [
            self._link(
                f"spacetime://calls/{call.id}",
                f"Call {call.id}: {call.function_name}",
                "Detailed captured function-call resource.",
            )
            for call in page[:10]
        ]
        return self._result(
            summary=f"Found {len(filtered)} matching call(s); returning {len(page)}.",
            data={
                "calls": items,
                "filters": {
                    "query": query,
                    "session_id": session_id,
                    "branch_id": branch_id,
                    "outcome": outcome,
                },
            },
            links=links,
            returned=len(page),
            total=len(filtered),
            truncated=truncated,
            next_cursor=str(next_offset) if truncated else None,
            warnings=warnings,
        )

    def execution_slice(
        self,
        *,
        branch_id: int,
        start_position: int = 0,
        end_position: int | None = None,
        around_step_id: int | None = None,
        radius: int = 5,
        cursor: str | None = None,
        response_format: ResponseFormat = "concise",
    ) -> AgentResult:
        """Return a bounded range from the complete inherited branch path."""

        self._validate_response_format(response_format)
        if cursor is not None:
            if (
                start_position != 0
                or end_position is not None
                or around_step_id is not None
            ):
                raise ValueError(
                    "cursor cannot be combined with a position range or around_step_id"
                )
            start_position = self._cursor_offset(cursor)
        if start_position < 0:
            raise ValueError("start_position must be greater than or equal to zero")
        if end_position is not None and end_position < 0:
            raise ValueError("end_position must be greater than or equal to zero")
        if radius < 0 or radius > 25:
            raise ValueError("radius must be between 0 and 25")
        if around_step_id is not None and (
            start_position != 0 or end_position is not None
        ):
            raise ValueError(
                "around_step_id cannot be combined with an explicit position range"
            )

        self._prepare_read()
        with self._lock:
            branch = self.data.get_branch(branch_id, resolve=True)
            steps = branch.steps
            if around_step_id is not None:
                try:
                    centre = next(
                        index
                        for index, step in enumerate(steps)
                        if step.id == around_step_id
                    )
                except StopIteration as error:
                    raise TraceNotFoundError(
                        f"Step {around_step_id} is not on resolved branch {branch_id}"
                    ) from error
                first = max(0, centre - radius)
                requested_last = min(len(steps) - 1, centre + radius)
            else:
                first = start_position
                requested_last = (
                    min(len(steps) - 1, first + 19)
                    if end_position is None
                    else min(len(steps) - 1, end_position)
                )
            if first >= len(steps) and steps:
                raise ValueError(
                    f"start_position {first} is outside branch range 0..{len(steps) - 1}"
                )
            if requested_last < first and steps:
                raise ValueError("end_position must not be before start_position")

            bounded_last = min(requested_last, first + self.MAX_SLICE_STEPS - 1)
            page = steps[first : bounded_last + 1] if steps else ()
            items = [
                self._step_summary(
                    step,
                    resolved_position=first + index,
                    detailed=response_format == "detailed",
                )
                for index, step in enumerate(page)
            ]

        truncated = bool(steps and bounded_last < requested_last)
        more_after = bool(steps and bounded_last + 1 < len(steps))
        next_cursor = str(bounded_last + 1) if more_after else None
        warnings = []
        if truncated:
            warnings.append(
                f"The requested range exceeds {self.MAX_SLICE_STEPS} steps. "
                f"Continue with start_position={bounded_last + 1}."
            )
        return self._result(
            summary=(
                f"Resolved branch {branch_id} has {len(steps)} step(s); "
                f"returning resolved positions {first}..{bounded_last}."
                if items
                else f"Resolved branch {branch_id} contains no steps."
            ),
            data={
                "branch": self._branch_summary(branch),
                "range": {
                    "start_position": first,
                    "end_position": bounded_last if items else None,
                    "positions_are_resolved_path_positions": True,
                },
                "steps": items,
            },
            links=[
                self._link(
                    f"spacetime://branches/{branch_id}",
                    f"Branch {branch_id}",
                    "Resolved branch metadata and bounded step index.",
                ),
                *[
                    self._link(
                        f"spacetime://steps/{step.id}",
                        f"Step {step.id}",
                        "Detailed recorded-step resource.",
                    )
                    for step in page[:10]
                ],
            ],
            returned=len(items),
            total=len(steps),
            truncated=truncated or more_after,
            next_cursor=next_cursor,
            warnings=warnings,
        )

    def inspect_step(
        self,
        step_id: int,
        *,
        response_format: ResponseFormat = "concise",
    ) -> AgentResult:
        """Inspect one exact recorded checkpoint with bounded state previews."""

        self._validate_response_format(response_format)
        detailed = response_format == "detailed"
        self._prepare_read()
        with self._lock:
            step = self.data.get_step(step_id)
            branch = self.data.get_branch(step.branch_id)
            call = self._step_call(step)
            source_line = (
                step.stack_snapshot.line_number
                if step.stack_snapshot is not None
                else call.first_line_number
                if call is not None
                else None
            )
            definition_id = (
                step.stack_snapshot.code_definition_id
                if step.stack_snapshot is not None
                else call.code_definition_id
                if call is not None
                else None
            )
            definition = (
                self.data.get_code_definition(definition_id)
                if definition_id is not None
                else None
            )
            if step.stack_snapshot is not None:
                local_references = step.stack_snapshot.local_references
                global_references = step.stack_snapshot.global_references
            elif call is not None:
                local_references = call.entry_local_references
                global_references = call.entry_global_references
            else:
                local_references = {}
                global_references = {}

            locals_payload, local_warning = self._state_mapping(local_references)
            globals_payload, global_warning = self._state_mapping(
                global_references,
                filter_dunder=True,
            )
            payload = self._step_summary(step, detailed=True)
            payload.update(
                {
                    "session_id": branch.session_id,
                    "branch": self._branch_summary(branch),
                    "locals": locals_payload,
                    "globals": globals_payload,
                    "source": self._source_excerpt(
                        definition,
                        line_number=source_line,
                        context_lines=8 if detailed else 4,
                    ),
                }
            )

        warnings = [item for item in (local_warning, global_warning) if item]
        if not self.trust_stored_values and any(
            not item.is_primitive
            for item in self._value_summaries().values()
            if item.reference
            in set(local_references.values()) | set(global_references.values())
        ):
            warnings.append(
                "Non-primitive values are represented by type and reference only. "
                "The server was not started with trusted stored-value deserialization."
            )

        links = [
            self._link(
                f"spacetime://steps/{step.id}",
                f"Step {step.id}",
                "Stable resource representation of this recorded step.",
            ),
            self._link(
                f"spacetime://branches/{step.branch_id}",
                f"Branch {step.branch_id}",
                "Branch containing the recorded step.",
            ),
        ]
        if call is not None:
            links.append(
                self._link(
                    f"spacetime://calls/{call.id}",
                    f"Call {call.id}: {call.function_name}",
                    "Function call observed at this step.",
                )
            )
        if definition is not None:
            links.append(
                self._link(
                    f"spacetime://code/{definition.id}",
                    f"Source: {definition.qualified_name or definition.name}",
                    "Complete stored source definition.",
                    mime_type="text/x-python",
                )
            )
        return self._result(
            summary=(
                f"Step {step.id} is a {step.kind} checkpoint at recorded branch "
                f"position {step.position}."
            ),
            data={"step": payload},
            links=links,
            returned=1,
            total=1,
            warnings=warnings,
        )

    def inspect_call(
        self,
        call_id: int,
        *,
        max_depth: int = 2,
        snapshot_limit: int = 20,
        response_format: ResponseFormat = "concise",
    ) -> AgentResult:
        """Inspect call state, source, snapshots, caller, and a bounded callee tree."""

        self._validate_response_format(response_format)
        if max_depth < 0 or max_depth > self.MAX_TREE_DEPTH:
            raise ValueError(f"max_depth must be between 0 and {self.MAX_TREE_DEPTH}")
        selected_snapshot_limit = self._validate_limit(snapshot_limit, 100)
        detailed = response_format == "detailed"
        self._prepare_read()
        with self._lock:
            call = self.data.get_function_call(call_id)
            locations = self._call_locations().get(call_id, ())
            snapshots = self.data.list_stack_snapshots(call_id)
            page = snapshots[:selected_snapshot_limit]
            definition = (
                self.data.get_code_definition(call.code_definition_id)
                if call.code_definition_id is not None
                else None
            )
            caller = (
                self.data.get_function_call(call.caller_call_id)
                if call.caller_call_id is not None
                else None
            )
            locals_payload, local_warning = self._state_mapping(
                call.entry_local_references
            )
            globals_payload, global_warning = self._state_mapping(
                call.entry_global_references,
                filter_dunder=True,
            )
            node_budget = [self.MAX_TREE_NODES]
            tree, tree_truncated = self._call_tree(
                call,
                max_depth=max_depth,
                depth=0,
                budget=node_budget,
            )
            payload = self._call_summary(
                call,
                locations=locations,
                snapshot_count=len(snapshots),
                detailed=True,
            )
            payload.update(
                {
                    "attributes": call.attributes,
                    "locals": locals_payload,
                    "globals": globals_payload,
                    "return_value": self._value_payload(call.return_reference),
                    "exception": self._value_payload(call.exception_reference),
                    "caller": (
                        self._call_summary(caller, detailed=False)
                        if caller is not None
                        else None
                    ),
                    "callee_tree": tree,
                    "snapshots": [
                        self._snapshot_summary(snapshot, detailed=detailed)
                        for snapshot in page
                    ],
                    "source": self._source_excerpt(
                        definition,
                        line_number=call.first_line_number,
                        context_lines=10 if detailed else 5,
                    ),
                }
            )

        warnings = [item for item in (local_warning, global_warning) if item]
        if len(snapshots) > len(page):
            warnings.append(
                f"Snapshots are truncated to {len(page)} of {len(snapshots)}. "
                "Increase snapshot_limit up to 100 or inspect their corresponding steps."
            )
        if tree_truncated:
            warnings.append(
                "The callee tree reached its depth or node bound. Increase max_depth "
                f"up to {self.MAX_TREE_DEPTH} or inspect a returned child call directly."
            )

        links = [
            self._link(
                f"spacetime://calls/{call.id}",
                f"Call {call.id}: {call.function_name}",
                "Stable resource representation of this function call.",
            )
        ]
        if definition is not None:
            links.append(
                self._link(
                    f"spacetime://code/{definition.id}",
                    f"Source: {definition.qualified_name or definition.name}",
                    "Complete stored source definition.",
                    mime_type="text/x-python",
                )
            )
        links.extend(
            self._link(
                f"spacetime://steps/{step_id}",
                f"Step {step_id}",
                "Recorded step containing this call or one of its observations.",
            )
            for _, _, step_id in locations[:10]
        )
        return self._result(
            summary=(
                f"Call {call.id} to {call.qualified_name or call.function_name} "
                f"has outcome {call.outcome!r} and {len(snapshots)} snapshot(s)."
            ),
            data={"call": payload},
            links=links,
            returned=1,
            total=1,
            truncated=bool(len(snapshots) > len(page) or tree_truncated),
            warnings=warnings,
        )

    def session_resource(self, session_id: int) -> dict[str, Any]:
        """Return complete session metadata and branch summaries."""

        self._prepare_read()
        with self._lock:
            session = self.data.get_session(session_id)
        return self._session_summary(session, detailed=True)

    def branch_resource(self, branch_id: int) -> dict[str, Any]:
        """Return resolved branch metadata with a bounded step index."""

        self._prepare_read()
        with self._lock:
            branch = self.data.get_branch(branch_id, resolve=True)
            page = branch.steps[: self.MAX_RESOURCE_STEPS]
        return {
            **self._branch_summary(branch),
            "resolved": True,
            "recipe": branch.recipe,
            "attributes": branch.attributes,
            "steps": [
                self._step_summary(step, resolved_position=index, detailed=False)
                for index, step in enumerate(page)
            ],
            "step_index_truncated": len(branch.steps) > len(page),
            "next_start_position": len(page) if len(branch.steps) > len(page) else None,
        }

    def step_resource(self, step_id: int) -> dict[str, Any]:
        """Return the detailed payload used by step inspection."""

        return self.inspect_step(step_id, response_format="detailed")["data"]["step"]

    def call_resource(self, call_id: int) -> dict[str, Any]:
        """Return the detailed, bounded payload used by call inspection."""

        return self.inspect_call(
            call_id,
            max_depth=2,
            snapshot_limit=50,
            response_format="detailed",
        )["data"]["call"]

    def code_resource(self, definition_id: str) -> str:
        """Return the exact source stored with a VM code definition."""

        self._prepare_read()
        with self._lock:
            return self.data.get_code_definition(definition_id).code_content

    def _prepare_read(self) -> None:
        if not self.refresh_before_read:
            return
        with self._lock:
            self.data.refresh()
            self._stored_values = None
            self._locations = None

    @staticmethod
    def _validate_response_format(response_format: str) -> None:
        if response_format not in {"concise", "detailed"}:
            raise ValueError("response_format must be 'concise' or 'detailed'")

    @staticmethod
    def _validate_limit(limit: int, maximum: int) -> int:
        if limit < 1 or limit > maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return limit

    @staticmethod
    def _cursor_offset(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            offset = int(cursor)
        except ValueError as error:
            raise ValueError(
                "cursor must be the next_cursor returned by this tool"
            ) from error
        if offset < 0:
            raise ValueError("cursor must represent a non-negative offset")
        return offset

    @staticmethod
    def _result(
        *,
        summary: str,
        data: dict[str, Any],
        links: Iterable[ResourceLinkPayload] = (),
        returned: int = 0,
        total: int | None = None,
        truncated: bool = False,
        next_cursor: str | None = None,
        warnings: Iterable[str] = (),
    ) -> AgentResult:
        return {
            "summary": summary,
            "data": data,
            "resource_links": list(links),
            "pagination": {
                "returned": returned,
                "total": total,
                "truncated": truncated,
                "next_cursor": next_cursor,
            },
            "warnings": list(warnings),
        }

    @staticmethod
    def _link(
        uri: str,
        name: str,
        description: str,
        *,
        mime_type: str = "application/json",
    ) -> ResourceLinkPayload:
        return {
            "uri": uri,
            "name": name,
            "description": description,
            "mime_type": mime_type,
        }

    @staticmethod
    def _session_summary(session: Any, *, detailed: bool = False) -> dict[str, Any]:
        payload = {
            "id": session.id,
            "name": session.name,
            "description": session.description,
            "step_kind": session.step_kind,
            "status": session.status,
            "created_at": _iso(session.created_at),
            "completed_at": _iso(session.completed_at),
            "root_branch_id": session.root_branch_id,
            "branch_count": len(session.branches),
            "branches": [
                {
                    "id": branch.id,
                    "name": branch.name,
                    "status": branch.status,
                    "parent_branch_id": branch.parent_branch_id,
                    "forked_from_step_id": branch.forked_from_step_id,
                    "own_step_count": branch.own_step_count,
                    "configuration_key": branch.configuration_key,
                }
                for branch in session.branches
            ],
        }
        if detailed:
            payload["attributes"] = session.attributes
        return payload

    @staticmethod
    def _branch_summary(branch: BranchDTO) -> dict[str, Any]:
        return {
            "id": branch.id,
            "session_id": branch.session_id,
            "name": branch.name,
            "status": branch.status,
            "parent_branch_id": branch.parent_branch_id,
            "forked_from_step_id": branch.forked_from_step_id,
            "child_branch_ids": list(branch.child_branch_ids),
            "configuration_key": branch.configuration_key,
            "recorded_step_count": len(branch.steps),
            "created_at": _iso(branch.created_at),
            "completed_at": _iso(branch.completed_at),
        }

    def _step_summary(
        self,
        step: StepDTO,
        *,
        resolved_position: int | None = None,
        detailed: bool,
    ) -> dict[str, Any]:
        call = self._step_call(step)
        payload: dict[str, Any] = {
            "id": step.id,
            "recorded_branch_id": step.branch_id,
            "recorded_position": step.position,
            "resolved_position": resolved_position,
            "kind": step.kind,
            "source_step_id": step.source_step_id,
            "label": step.label,
            "created_at": _iso(step.created_at),
            "call": self._call_summary(call, detailed=False) if call else None,
            "snapshot": (
                self._snapshot_summary(step.stack_snapshot, detailed=False)
                if step.stack_snapshot is not None
                else None
            ),
            "external_interactions": [
                {
                    "id": occurrence.id,
                    "position": occurrence.position,
                    "call": self._call_summary(occurrence.call, detailed=False),
                }
                for occurrence in step.external_interactions
            ],
        }
        if detailed:
            payload["annotations"] = step.annotations
            if step.stack_snapshot is not None:
                payload["local_names"] = sorted(step.stack_snapshot.local_references)
                payload["global_names"] = sorted(
                    name
                    for name in step.stack_snapshot.global_references
                    if not (name.startswith("__") and name.endswith("__"))
                )
            elif call is not None:
                payload["local_names"] = sorted(call.entry_local_references)
                payload["global_names"] = sorted(
                    name
                    for name in call.entry_global_references
                    if not (name.startswith("__") and name.endswith("__"))
                )
        return payload

    @staticmethod
    def _call_summary(
        call: FunctionCallDTO,
        *,
        locations: tuple[tuple[int, int, int], ...] = (),
        snapshot_count: int = 0,
        detailed: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": call.id,
            "function_name": call.function_name,
            "qualified_name": call.qualified_name,
            "module_name": call.module_name,
            "file_path": call.file_path,
            "first_line_number": call.first_line_number,
            "outcome": call.outcome,
            "caller_call_id": call.caller_call_id,
            "code_definition_id": call.code_definition_id,
        }
        if detailed:
            payload.update(
                {
                    "started_at": _iso(call.started_at),
                    "completed_at": _iso(call.completed_at),
                    "duration_seconds": _duration(
                        call.started_at,
                        call.completed_at,
                    ),
                    "snapshot_count": snapshot_count,
                    "locations": [
                        {
                            "session_id": session_id,
                            "branch_id": branch_id,
                            "step_id": step_id,
                        }
                        for session_id, branch_id, step_id in locations
                    ],
                }
            )
        elif locations:
            payload["locations"] = [
                {
                    "session_id": session_id,
                    "branch_id": branch_id,
                    "step_id": step_id,
                }
                for session_id, branch_id, step_id in locations[:3]
            ]
        return payload

    @staticmethod
    def _snapshot_summary(
        snapshot: StackSnapshotDTO,
        *,
        detailed: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": snapshot.id,
            "function_call_id": snapshot.function_call_id,
            "line_number": snapshot.line_number,
            "code_definition_id": snapshot.code_definition_id,
        }
        if detailed:
            payload.update(
                {
                    "instruction_offset": snapshot.instruction_offset,
                    "captured_at": _iso(snapshot.captured_at),
                    "local_names": sorted(snapshot.local_references),
                    "global_names": sorted(
                        name
                        for name in snapshot.global_references
                        if not (name.startswith("__") and name.endswith("__"))
                    ),
                    "attributes": snapshot.attributes,
                }
            )
        return payload

    def _state_mapping(
        self,
        references: dict[str, str],
        *,
        filter_dunder: bool = False,
    ) -> tuple[dict[str, Any], str | None]:
        selected = [
            (name, reference)
            for name, reference in references.items()
            if not filter_dunder or not (name.startswith("__") and name.endswith("__"))
        ]
        page = selected[: self.MAX_STATE_NAMES]
        payload = {name: self._value_payload(reference) for name, reference in page}
        warning = None
        if len(selected) > len(page):
            warning = (
                f"State is truncated to {len(page)} of {len(selected)} names. "
                "Use a narrower captured function or ignored_names for irrelevant state."
            )
        return payload, warning

    def _value_payload(self, reference: str | None) -> dict[str, Any]:
        if reference is None:
            return {"reference": None, "type": "NoneType", "value": None}
        summary = self._value_summaries().get(reference)
        if summary is None:
            return {
                "reference": reference,
                "error": "Stored-value metadata is missing",
            }
        payload: dict[str, Any] = {
            "reference": summary.reference,
            "type": summary.type_name,
            "identity_id": summary.identity_id,
            "version": summary.version,
            "is_primitive": summary.is_primitive,
        }
        if summary.is_primitive or self.trust_stored_values:
            try:
                payload["value"] = _bounded_preview(
                    self.data.get_stored_value(reference).value
                )
            except Exception as error:  # noqa: BLE001 - preserve the rest of the trace
                payload["error"] = (
                    f"{type(error).__name__}: stored value could not be materialized"
                )
        else:
            payload["value"] = f"<{summary.type_name}: preview disabled>"
        return payload

    def _value_summaries(self) -> dict[str, StoredValueSummaryDTO]:
        with self._lock:
            if self._stored_values is None:
                self._stored_values = {
                    item.reference: item for item in self.data.list_stored_values()
                }
            return self._stored_values

    def _source_excerpt(
        self,
        definition: CodeDefinitionDTO | None,
        *,
        line_number: int | None,
        context_lines: int,
    ) -> dict[str, Any] | None:
        if definition is None:
            return None
        lines = definition.code_content.splitlines()
        first_line = definition.first_line_number or 1
        if not lines:
            start_index = 0
            end_index = 0
        elif line_number is None:
            start_index = 0
            end_index = min(len(lines), 2 * context_lines + 1)
        else:
            selected_index = max(0, min(len(lines) - 1, line_number - first_line))
            start_index = max(0, selected_index - context_lines)
            end_index = min(len(lines), selected_index + context_lines + 1)
        excerpt = "\n".join(
            f"{first_line + index:>5} | {lines[index]}"
            for index in range(start_index, end_index)
        )
        return {
            "definition_id": definition.id,
            "name": definition.qualified_name or definition.name,
            "module_path": definition.module_path,
            "focus_line": line_number,
            "start_line": first_line + start_index,
            "end_line": first_line + end_index - 1 if end_index else first_line,
            "excerpt": excerpt,
            "resource_uri": f"spacetime://code/{definition.id}",
        }

    def _call_tree(
        self,
        call: FunctionCallDTO,
        *,
        max_depth: int,
        depth: int,
        budget: list[int],
    ) -> tuple[dict[str, Any], bool]:
        budget[0] -= 1
        children = self.data.list_callee_calls(call.id)
        if depth >= max_depth or budget[0] <= 0:
            return {
                **self._call_summary(call, detailed=False),
                "children": [],
                "child_count": len(children),
                "children_truncated": bool(children),
            }, bool(children)

        result_children = []
        truncated = False
        for child in children:
            if budget[0] <= 0:
                truncated = True
                break
            child_payload, child_truncated = self._call_tree(
                child,
                max_depth=max_depth,
                depth=depth + 1,
                budget=budget,
            )
            result_children.append(child_payload)
            truncated = truncated or child_truncated
        if len(result_children) < len(children):
            truncated = True
        return {
            **self._call_summary(call, detailed=False),
            "children": result_children,
            "child_count": len(children),
            "children_truncated": len(result_children) < len(children),
        }, truncated

    def _call_locations(self) -> dict[int, tuple[tuple[int, int, int], ...]]:
        if self._locations is not None:
            return self._locations
        locations: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        sessions = self.data.list_sessions()
        for session in sessions:
            for branch_summary in session.branches:
                branch = self.data.get_branch(branch_summary.id)
                for step in branch.steps:
                    location = (session.id, branch.id, step.id)
                    for call_id in self._step_call_ids(step):
                        if location not in locations[call_id]:
                            locations[call_id].append(location)

        calls = self.data.list_function_calls()
        changed = True
        while changed:
            changed = False
            for call in calls:
                parent_locations = locations.get(call.caller_call_id or -1)
                if call.id in locations or not parent_locations:
                    continue
                locations[call.id].extend(parent_locations)
                changed = True
        self._locations = {
            identifier: tuple(items) for identifier, items in locations.items()
        }
        return self._locations

    def _step_call(self, step: StepDTO) -> FunctionCallDTO | None:
        if step.function_call is not None:
            return step.function_call
        if step.stack_snapshot is not None:
            return self.data.get_function_call(step.stack_snapshot.function_call_id)
        return None

    @staticmethod
    def _step_call_ids(step: StepDTO) -> tuple[int, ...]:
        ids = []
        if step.function_call is not None:
            ids.append(step.function_call.id)
        elif step.stack_snapshot is not None:
            ids.append(step.stack_snapshot.function_call_id)
        ids.extend(item.call.id for item in step.external_interactions)
        return tuple(ids)


__all__ = [
    "AgentResult",
    "AgentTraceService",
    "PaginationPayload",
    "ResourceLinkPayload",
    "ResponseFormat",
]
