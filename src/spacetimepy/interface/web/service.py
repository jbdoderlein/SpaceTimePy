"""Transport-neutral presentation service used by HTTP and browser clients."""

from __future__ import annotations

import base64
from collections import Counter, defaultdict
from threading import RLock
from typing import TYPE_CHECKING, Any

from spacetimepy.interface.alignment import AlignmentService

if TYPE_CHECKING:
    import datetime

    from spacetimepy.interface.alignment import AlignmentResult
    from spacetimepy.interface.data import (
        BranchDTO,
        CodeDefinitionDTO,
        FunctionCallCapturePerformanceDTO,
        FunctionCallDTO,
        SessionDTO,
        StackSnapshotDTO,
        StepDTO,
        TraceData,
    )


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _duration(
    started_at: datetime.datetime,
    completed_at: datetime.datetime | None,
) -> float | None:
    if completed_at is None:
        return None
    return (completed_at - started_at).total_seconds()


def _preview(value: Any, *, depth: int = 0) -> Any:
    """Produce bounded JSON-compatible data without trusting object reprs."""

    if value is None or isinstance(value, bool | int | float | str):
        if isinstance(value, str) and len(value) > 10_000:
            return value[:9_997] + "..."
        return value
    if depth >= 2:
        return f"<{type(value).__name__}>"
    if isinstance(value, bytes):
        if value.startswith(b"\x89PNG\r\n\x1a\n"):
            return {
                "encoding": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(value).decode("ascii"),
            }
        if value.startswith(b"\xff\xd8\xff"):
            return {
                "encoding": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(value).decode("ascii"),
            }
        return f"<bytes: {len(value)} bytes>"
    if isinstance(value, list | tuple | set | frozenset):
        selected = list(value)
        result = [_preview(item, depth=depth + 1) for item in selected[:20]]
        if len(selected) > 20:
            result.append(f"... {len(selected) - 20} more")
        return result
    if isinstance(value, dict):
        items = list(value.items())
        result = {str(key): _preview(item, depth=depth + 1) for key, item in items[:20]}
        if len(items) > 20:
            result["..."] = f"{len(items) - 20} more"
        return result
    try:
        text = repr(value)
    except Exception as error:  # noqa: BLE001 - presentation must be resilient
        text = f"<unrepresentable: {type(error).__name__}: {error}>"
    return text if len(text) <= 10_000 else text[:9_997] + "..."


class TraceService:
    """Explore one trace through public DTOs and safe presentation payloads."""

    def __init__(
        self,
        data: TraceData,
        alignment: AlignmentService | None = None,
    ) -> None:
        self.data = data
        self.alignment = alignment or AlignmentService(data)
        self._lock = RLock()

    def refresh(self) -> dict[str, bool]:
        with self._lock:
            self.data.refresh()
        return {"refreshed": True}

    def database_info(self) -> dict[str, Any]:
        with self._lock:
            statistics = self.data.get_statistics()
        return {
            "database": self.data.database_label,
            "session_count": statistics.session_count,
            "branch_count": statistics.branch_count,
            "step_count": statistics.step_count,
            "function_call_count": statistics.function_call_count,
            "profiled_function_call_count": (
                statistics.function_call_capture_performance_count
            ),
            "stack_snapshot_count": statistics.stack_snapshot_count,
            "external_interaction_count": statistics.external_interaction_count,
            "object_identity_count": statistics.object_identity_count,
            "stored_object_count": statistics.stored_value_count,
            "code_definition_count": statistics.code_definition_count,
        }

    def sessions(self) -> dict[str, Any]:
        with self._lock:
            sessions = self.data.list_sessions()
        return {"sessions": [self._session_summary(item) for item in sessions]}

    def session(self, session_id: int) -> dict[str, Any]:
        with self._lock:
            session = self.data.get_session(session_id)
            branches = [self.data.get_branch(item.id) for item in session.branches]
            call_ids: list[int] = []
            for branch in branches:
                call_ids.extend(self._step_call_ids(branch.steps))
            unique_call_ids = tuple(dict.fromkeys(call_ids))
            calls = [
                self.data.get_function_call(call_id) for call_id in unique_call_ids
            ]
            function_counts = Counter(call.function_name for call in calls)

            return {
                **self._session_summary(session),
                "attributes": session.attributes,
                "metadata": session.attributes,
                "branches": [self._branch_summary(branch) for branch in branches],
                "function_calls": [self._call_payload(call) for call in calls],
                "function_call_ids": list(unique_call_ids),
                "function_count": dict(function_counts),
                "common_variables": self._common_variables(calls),
            }

    def branch(self, branch_id: int, *, resolve: bool = False) -> dict[str, Any]:
        with self._lock:
            branch = self.data.get_branch(branch_id, resolve=resolve)
            return self._branch_payload(branch)

    def step(self, step_id: int) -> dict[str, Any]:
        with self._lock:
            step = self.data.get_step(step_id)
            return self._step_payload(step, include_values=True)

    def code_definition(self, definition_id: str) -> dict[str, Any]:
        """Return one stored source definition for on-demand code views."""

        with self._lock:
            definition = self.data.get_code_definition(definition_id)
        return {"code_definition": self._code_payload(definition)}

    def alignment_algorithms(self) -> dict[str, Any]:
        algorithms = self.alignment.registry.algorithms()
        return {
            "algorithms": [
                {
                    "name": algorithm.name,
                    "version": algorithm.version,
                    "offline": algorithm.offline,
                    "online": algorithm.online,
                }
                for algorithm in algorithms
            ]
        }

    def compare_alignment(
        self,
        *,
        reference_branch_id: int,
        target_branch_id: int,
        algorithm: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Calculate and serialize one alignment without retaining it."""

        with self._lock:
            result = self.alignment.compare(
                reference_branch_id=reference_branch_id,
                target_branch_id=target_branch_id,
                algorithm=algorithm,
                options=options,
            )
        if result is None:
            raise ValueError(
                "No default alignment exists for this trace granularity; "
                "select an algorithm"
            )
        return {"alignment": self._alignment_payload(result)}

    def function_calls(
        self,
        *,
        search: str = "",
        file: str = "",
        function: str = "",
        session_id: int | None = None,
        branch_id: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            calls = list(self.data.list_function_calls())
            snapshots = self.data.list_stack_snapshots()
            locations = self._call_locations()

        snapshot_counts = Counter(item.function_call_id for item in snapshots)
        lowered_search = search.casefold()
        lowered_file = file.casefold()
        lowered_function = function.casefold()

        def selected(call: FunctionCallDTO) -> bool:
            location = locations.get(call.id, ())
            if session_id is not None and not any(
                item[0] == session_id for item in location
            ):
                return False
            if branch_id is not None and not any(
                item[1] == branch_id for item in location
            ):
                return False
            if lowered_file and lowered_file not in (call.file_path or "").casefold():
                return False
            names = f"{call.function_name} {call.qualified_name or ''}".casefold()
            if lowered_function and lowered_function not in names:
                return False
            haystack = f"{names} {call.file_path or ''} {call.module_name or ''}"
            return not lowered_search or lowered_search in haystack.casefold()

        filtered = [call for call in calls if selected(call)]
        page = filtered[offset : offset + limit]
        return {
            "function_calls": [
                self._call_summary(
                    call,
                    snapshot_count=snapshot_counts[call.id],
                    locations=locations.get(call.id, ()),
                )
                for call in page
            ],
            "total": len(filtered),
            "limit": limit,
            "offset": offset,
        }

    def function_call(self, call_id: int) -> dict[str, Any]:
        with self._lock:
            call = self.data.get_function_call(call_id)
            snapshots = self.data.list_stack_snapshots(call_id)
            calls = self.data.list_function_calls()
            locations = self._call_locations().get(call_id, ())
            call_index = next(
                (
                    index
                    for index, candidate in enumerate(calls)
                    if candidate.id == call_id
                ),
                None,
            )
            payload = self._call_payload(call)
            payload.update(
                {
                    "has_stack_recording": bool(snapshots),
                    "stack_recording": [
                        self._snapshot_summary(snapshot) for snapshot in snapshots
                    ],
                    "locations": [self._location_payload(item) for item in locations],
                    "prev_call": (
                        calls[call_index - 1].id
                        if call_index is not None and call_index > 0
                        else None
                    ),
                    "next_call": (
                        calls[call_index + 1].id
                        if call_index is not None and call_index + 1 < len(calls)
                        else None
                    ),
                }
            )
            return {"function_call": payload}

    def function_call_performance(self, call_id: int) -> dict[str, Any]:
        """Return optional capture-overhead metrics for one function call."""

        with self._lock:
            performance = self.data.get_function_call_performance(call_id)
        return {
            "function_call_id": call_id,
            "capture_performance": self._capture_performance_payload(performance),
        }

    def stack_recording(self, call_id: int) -> dict[str, Any]:
        with self._lock:
            call = self.data.get_function_call(call_id)
            snapshots = self.data.list_stack_snapshots(call_id)
            definition_ids = {
                identifier
                for identifier in [
                    call.code_definition_id,
                    *[snapshot.code_definition_id for snapshot in snapshots],
                ]
                if identifier is not None
            }
            definitions = {
                identifier: self._code_payload(
                    self.data.get_code_definition(identifier)
                )
                for identifier in definition_ids
            }
            frames = []
            for position, snapshot in enumerate(snapshots):
                frame = self._snapshot_payload(snapshot)
                frame.update(
                    {
                        "position": position,
                        "snapshot_id": snapshot.id,
                        "previous_snapshot_id": (
                            snapshots[position - 1].id if position > 0 else None
                        ),
                        "next_snapshot_id": (
                            snapshots[position + 1].id
                            if position + 1 < len(snapshots)
                            else None
                        ),
                    }
                )
                frames.append(frame)

            code = definitions.get(call.code_definition_id)
            return {
                "function": {
                    **self._call_summary(call, snapshot_count=len(snapshots)),
                    "code": code,
                },
                "code_definitions": definitions,
                "frames": frames,
            }

    def snapshot(self, snapshot_id: int) -> dict[str, Any]:
        with self._lock:
            snapshot = self.data.get_stack_snapshot(snapshot_id)
            return {"snapshot": self._snapshot_payload(snapshot)}

    def execution_tree(self, call_id: int, *, max_depth: int = 5) -> dict[str, Any]:
        with self._lock:
            root = self.data.get_function_call(call_id)

            def build(call: FunctionCallDTO, depth: int) -> dict[str, Any]:
                children = (
                    self.data.list_callee_calls(call.id) if depth < max_depth else ()
                )
                return {
                    **self._call_summary(call),
                    "children": [build(child, depth + 1) for child in children],
                    "truncated": depth >= max_depth
                    and bool(self.data.list_callee_calls(call.id)),
                }

            tree = build(root, 0)
        return {"execution_tree": tree}

    def trace_parts(self, call_id: int) -> dict[str, Any]:
        recording = self.stack_recording(call_id)
        parts: list[dict[str, Any]] = []
        for frame in recording["frames"]:
            definition_id = frame.get("code_definition_id")
            if not parts or parts[-1]["code_definition_id"] != definition_id:
                parts.append(
                    {
                        "part_id": f"part-{len(parts)}",
                        "code_definition_id": definition_id,
                        "code": recording["code_definitions"].get(definition_id),
                        "frames": [],
                    }
                )
            parts[-1]["frames"].append(frame)
        for part in parts:
            part["frame_count"] = len(part["frames"])
        return {"function": recording["function"], "parts": parts}

    def function_graph(self, call_id: int) -> dict[str, Any]:
        recording = self.stack_recording(call_id)
        nodes = {}
        edges = []
        for position, frame in enumerate(recording["frames"]):
            node_id = f"snapshot:{frame['id']}"
            nodes[node_id] = {
                "type": "snapshot",
                "line": frame["line"],
                "position": position,
                "locals": frame.get("locals", {}),
                "globals": frame.get("globals", {}),
            }
            if position:
                edges.append(
                    [
                        f"snapshot:{recording['frames'][position - 1]['id']}",
                        node_id,
                        {"type": "next"},
                    ]
                )
        return {"nodes": nodes, "edges": edges}

    def object_graph(
        self,
        *,
        show_isolated: bool = False,
        max_nodes: int = 2_000,
    ) -> dict[str, Any]:
        with self._lock:
            sessions = self.data.list_sessions()
            branches = [
                self.data.get_branch(branch.id)
                for session in sessions
                for branch in session.branches
            ]
            calls = self.data.list_function_calls()
            snapshots = self.data.list_stack_snapshots()
            definitions = self.data.list_code_definitions()
            stored_values = self.data.list_stored_values()
            calls_by_id = {call.id: call for call in calls}

        nodes: dict[str, dict[str, Any]] = {}
        edges: list[list[Any]] = []

        def node(identifier: str, **data: Any) -> None:
            if len(nodes) < max_nodes or identifier in nodes:
                nodes[identifier] = {"id": identifier, **data}

        def edge(source: str, target: str, kind: str, label: str = "") -> None:
            if source in nodes and target in nodes:
                edges.append([source, target, {"type": kind, "label": label}])

        for session in sessions:
            node(
                f"session:{session.id}",
                type="session",
                label=session.name or f"Session {session.id}",
            )
        for branch in branches:
            branch_id = f"branch:{branch.id}"
            node(branch_id, type="branch", label=branch.name or f"Branch {branch.id}")
            edge(f"session:{branch.session_id}", branch_id, "contains")
            if branch.parent_branch_id is not None:
                edge(f"branch:{branch.parent_branch_id}", branch_id, "fork")
            for step in branch.steps:
                step_id = f"step:{step.id}"
                node(step_id, type="step", label=step.label or f"Step {step.position}")
                edge(branch_id, step_id, "contains")
                call = (
                    step.function_call
                    if step.function_call is not None
                    else calls_by_id.get(step.stack_snapshot.function_call_id)
                    if step.stack_snapshot is not None
                    else None
                )
                if call is not None:
                    node(
                        f"call:{call.id}",
                        type="function_call",
                        label=call.function_name,
                    )
                    edge(step_id, f"call:{call.id}", "observes")
                if step.stack_snapshot is not None:
                    snapshot_id = f"snapshot:{step.stack_snapshot.id}"
                    node(
                        snapshot_id,
                        type="snapshot",
                        label=f"Line {step.stack_snapshot.line_number}",
                    )
                    edge(step_id, snapshot_id, "observes")
                for occurrence in step.external_interactions:
                    external = occurrence.call
                    node(
                        f"call:{external.id}",
                        type="external_call",
                        label=external.function_name,
                    )
                    edge(step_id, f"call:{external.id}", "external")

        for definition in definitions:
            node(f"code:{definition.id}", type="code", label=definition.name)
        for stored in stored_values:
            node(
                f"value:{stored.reference}",
                type="stored_value",
                label=f"{stored.type_name} v{stored.version}",
            )
        for call in calls:
            call_id = f"call:{call.id}"
            node(call_id, type="function_call", label=call.function_name)
            if call.caller_call_id is not None:
                edge(f"call:{call.caller_call_id}", call_id, "calls")
            if call.code_definition_id is not None:
                edge(call_id, f"code:{call.code_definition_id}", "defined_by")
            for name, reference in call.entry_local_references.items():
                edge(call_id, f"value:{reference}", "local", name)
            for name, reference in call.entry_global_references.items():
                edge(call_id, f"value:{reference}", "global", name)
            if call.return_reference is not None:
                edge(call_id, f"value:{call.return_reference}", "return")
            if call.exception_reference is not None:
                edge(call_id, f"value:{call.exception_reference}", "exception")
        for snapshot in snapshots:
            snapshot_id = f"snapshot:{snapshot.id}"
            node(snapshot_id, type="snapshot", label=f"Line {snapshot.line_number}")
            edge(f"call:{snapshot.function_call_id}", snapshot_id, "snapshot")
            if snapshot.code_definition_id is not None:
                edge(snapshot_id, f"code:{snapshot.code_definition_id}", "defined_by")
            for name, reference in snapshot.local_references.items():
                edge(snapshot_id, f"value:{reference}", "local", name)
            for name, reference in snapshot.global_references.items():
                edge(snapshot_id, f"value:{reference}", "global", name)

        if not show_isolated:
            connected = {
                endpoint for graph_edge in edges for endpoint in graph_edge[:2]
            }
            nodes = {
                identifier: data
                for identifier, data in nodes.items()
                if identifier in connected
            }
            edges = [
                graph_edge
                for graph_edge in edges
                if graph_edge[0] in nodes and graph_edge[1] in nodes
            ]
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": len(nodes) >= max_nodes,
        }

    def _session_summary(self, session: SessionDTO) -> dict[str, Any]:
        return {
            "id": session.id,
            "name": session.name,
            "description": session.description,
            "step_kind": session.step_kind,
            "status": session.status,
            "created_at": _iso(session.created_at),
            "start_time": _iso(session.created_at),
            "completed_at": _iso(session.completed_at),
            "end_time": _iso(session.completed_at),
            "duration": _duration(session.created_at, session.completed_at),
            "root_branch_id": session.root_branch_id,
            "branch_count": len(session.branches),
        }

    def _branch_summary(self, branch: BranchDTO) -> dict[str, Any]:
        return {
            "id": branch.id,
            "session_id": branch.session_id,
            "parent_branch_id": branch.parent_branch_id,
            "forked_from_step_id": branch.forked_from_step_id,
            "child_branch_ids": list(branch.child_branch_ids),
            "name": branch.name,
            "status": branch.status,
            "configuration_key": branch.configuration_key,
            "created_at": _iso(branch.created_at),
            "completed_at": _iso(branch.completed_at),
            "step_count": len(branch.steps),
        }

    def _branch_payload(self, branch: BranchDTO) -> dict[str, Any]:
        return {
            **self._branch_summary(branch),
            "recipe": branch.recipe,
            "attributes": branch.attributes,
            "resolved": branch.is_resolved_path,
            "steps": [self._step_payload(step) for step in branch.steps],
        }

    @staticmethod
    def _alignment_payload(result: AlignmentResult) -> dict[str, Any]:
        return {
            "algorithm": result.algorithm,
            "algorithm_version": result.algorithm_version,
            "reference_branch_id": result.reference_branch_id,
            "target_branch_id": result.target_branch_id,
            "links": [
                {
                    "reference_step_id": (
                        link.reference_step.id
                        if link.reference_step is not None
                        else None
                    ),
                    "target_step_id": (
                        link.target_step.id if link.target_step is not None else None
                    ),
                    "relation": link.relation.value,
                }
                for link in result.links
            ],
        }

    def _step_payload(
        self,
        step: StepDTO,
        *,
        include_values: bool = False,
    ) -> dict[str, Any]:
        call = self._step_call(step)
        payload = {
            "id": step.id,
            "branch_id": step.branch_id,
            "position": step.position,
            "kind": step.kind,
            "source_step_id": step.source_step_id,
            "label": step.label,
            "annotations": step.annotations,
            "created_at": _iso(step.created_at),
            "function_call": self._call_summary(call) if call is not None else None,
            "stack_snapshot": (
                self._snapshot_summary(step.stack_snapshot)
                if step.stack_snapshot is not None
                else None
            ),
            "external_interactions": [
                {
                    "id": occurrence.id,
                    "position": occurrence.position,
                    "call": self._call_summary(occurrence.call),
                }
                for occurrence in step.external_interactions
            ],
        }
        if include_values:
            if step.stack_snapshot is not None:
                payload["locals"] = self._reference_mapping(
                    step.stack_snapshot.local_references
                )
                payload["globals"] = self._reference_mapping(
                    step.stack_snapshot.global_references,
                    filter_dunder=True,
                )
            elif call is not None:
                payload["locals"] = self._reference_mapping(call.entry_local_references)
                payload["globals"] = self._reference_mapping(
                    call.entry_global_references,
                    filter_dunder=True,
                )
        return payload

    def _call_summary(
        self,
        call: FunctionCallDTO,
        *,
        snapshot_count: int = 0,
        locations: tuple[tuple[int, int, int], ...] = (),
    ) -> dict[str, Any]:
        return {
            "id": call.id,
            "function": call.function_name,
            "function_name": call.function_name,
            "qualified_name": call.qualified_name,
            "module_name": call.module_name,
            "file": call.file_path,
            "file_path": call.file_path,
            "line": call.first_line_number,
            "first_line_number": call.first_line_number,
            "start_time": _iso(call.started_at),
            "started_at": _iso(call.started_at),
            "end_time": _iso(call.completed_at),
            "completed_at": _iso(call.completed_at),
            "duration": _duration(call.started_at, call.completed_at),
            "outcome": call.outcome,
            "code_definition_id": call.code_definition_id,
            "caller_call_id": call.caller_call_id,
            "has_stack_recording": snapshot_count > 0,
            "snapshot_count": snapshot_count,
            "locations": [self._location_payload(item) for item in locations],
            "capture_performance": self._capture_performance_payload(
                call.capture_performance
            ),
        }

    def _call_payload(self, call: FunctionCallDTO) -> dict[str, Any]:
        return {
            **self._call_summary(call),
            "attributes": call.attributes,
            "call_metadata": call.attributes,
            "locals_refs": call.entry_local_references,
            "globals_refs": call.entry_global_references,
            "locals": self._reference_mapping(call.entry_local_references),
            "globals": self._reference_mapping(
                call.entry_global_references,
                filter_dunder=True,
            ),
            "return_reference": call.return_reference,
            "return_value": self._reference_payload(call.return_reference),
            "exception_reference": call.exception_reference,
            "exception": self._reference_payload(call.exception_reference),
        }

    def _snapshot_summary(self, snapshot: StackSnapshotDTO) -> dict[str, Any]:
        return {
            "id": snapshot.id,
            "function_call_id": snapshot.function_call_id,
            "code_definition_id": snapshot.code_definition_id,
            "line": snapshot.line_number,
            "line_number": snapshot.line_number,
            "instruction_offset": snapshot.instruction_offset,
            "timestamp": _iso(snapshot.captured_at),
            "captured_at": _iso(snapshot.captured_at),
            "attributes": snapshot.attributes,
        }

    @staticmethod
    def _capture_performance_payload(
        performance: FunctionCallCapturePerformanceDTO | None,
    ) -> dict[str, Any] | None:
        if performance is None:
            return None
        return {
            "function_call_id": performance.function_call_id,
            "clock": "perf_counter_ns",
            "unit": "nanoseconds",
            "start_capture_ns": performance.start_capture_ns,
            "return_capture_ns": performance.return_capture_ns,
            "unwind_capture_ns": performance.unwind_capture_ns,
            "line_capture_ns": performance.line_capture_ns,
            "direct_capture_ns": performance.direct_capture_ns,
            "inclusive_capture_ns": performance.inclusive_capture_ns,
            "direct_capture_ms": performance.direct_capture_ms,
            "inclusive_capture_ms": performance.inclusive_capture_ms,
            "line_event_count": performance.line_event_count,
            "line_snapshot_count": performance.line_snapshot_count,
            "filtered_line_event_count": performance.filtered_line_event_count,
            "line_capture_min_ns": performance.line_capture_min_ns,
            "line_capture_max_ns": performance.line_capture_max_ns,
            "line_capture_average_ns": performance.line_capture_average_ns,
        }

    def _snapshot_payload(self, snapshot: StackSnapshotDTO) -> dict[str, Any]:
        return {
            **self._snapshot_summary(snapshot),
            "locals_refs": snapshot.local_references,
            "globals_refs": snapshot.global_references,
            "locals": self._reference_mapping(snapshot.local_references),
            "globals": self._reference_mapping(
                snapshot.global_references,
                filter_dunder=True,
            ),
        }

    @staticmethod
    def _code_payload(definition: CodeDefinitionDTO) -> dict[str, Any]:
        return {
            "id": definition.id,
            "name": definition.name,
            "qualified_name": definition.qualified_name,
            "type": definition.kind,
            "kind": definition.kind,
            "module_path": definition.module_path,
            "content": definition.code_content,
            "code_content": definition.code_content,
            "first_line_no": definition.first_line_number,
            "first_line_number": definition.first_line_number,
            "created_at": _iso(definition.created_at),
        }

    def _reference_mapping(
        self,
        references: dict[str, str],
        *,
        filter_dunder: bool = False,
    ) -> dict[str, Any]:
        return {
            name: self._reference_payload(reference)
            for name, reference in references.items()
            if not filter_dunder or not (name.startswith("__") and name.endswith("__"))
        }

    def _reference_payload(self, reference: str | None) -> dict[str, Any]:
        if reference is None:
            return {"reference": None, "value": None, "type": "NoneType"}
        try:
            stored = self.data.get_stored_value(reference)
            value = _preview(stored.value)
            return {
                "reference": stored.reference,
                "identity_id": stored.identity_id,
                "version": stored.version,
                "type": stored.type_name,
                "is_primitive": stored.is_primitive,
                "value": value,
            }
        except Exception as error:  # noqa: BLE001 - keep other trace data usable
            return {
                "reference": reference,
                "type": "Error",
                "value": f"<{type(error).__name__}: {error}>",
                "error": True,
            }

    def _call_locations(self) -> dict[int, tuple[tuple[int, int, int], ...]]:
        locations: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        sessions = self.data.list_sessions()
        for session in sessions:
            for branch_summary in session.branches:
                branch = self.data.get_branch(branch_summary.id)
                for step in branch.steps:
                    for call_id in self._step_call_ids((step,)):
                        location = (session.id, branch.id, step.id)
                        if location not in locations[call_id]:
                            locations[call_id].append(location)

        calls = self.data.list_function_calls()
        changed = True
        while changed:
            changed = False
            for call in calls:
                if call.id in locations or call.caller_call_id not in locations:
                    continue
                locations[call.id].extend(locations[call.caller_call_id])
                changed = True
        return {identifier: tuple(items) for identifier, items in locations.items()}

    @staticmethod
    def _location_payload(location: tuple[int, int, int]) -> dict[str, int]:
        return {
            "session_id": location[0],
            "branch_id": location[1],
            "step_id": location[2],
        }

    def _step_call(self, step: StepDTO) -> FunctionCallDTO | None:
        if step.function_call is not None:
            return step.function_call
        if step.stack_snapshot is not None:
            return self.data.get_function_call(step.stack_snapshot.function_call_id)
        return None

    @classmethod
    def _step_call_ids(cls, steps: tuple[StepDTO, ...]) -> list[int]:
        result = []
        for step in steps:
            if step.function_call is not None:
                result.append(step.function_call.id)
            elif step.stack_snapshot is not None:
                result.append(step.stack_snapshot.function_call_id)
            result.extend(
                occurrence.call.id for occurrence in step.external_interactions
            )
        return result

    @staticmethod
    def _common_variables(calls: list[FunctionCallDTO]) -> dict[str, Any]:
        grouped: dict[str, list[FunctionCallDTO]] = defaultdict(list)
        for call in calls:
            grouped[call.function_name].append(call)
        result = {}
        for name, function_calls in grouped.items():
            local_names = set(function_calls[0].entry_local_references)
            global_names = set(function_calls[0].entry_global_references)
            for call in function_calls[1:]:
                local_names.intersection_update(call.entry_local_references)
                global_names.intersection_update(call.entry_global_references)
            result[name] = {
                "locals": sorted(local_names),
                "globals": sorted(global_names),
            }
        return result


__all__ = ["TraceService"]
