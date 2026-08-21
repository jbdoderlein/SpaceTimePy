from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from spacetimepy import (
    STACK_SNAPSHOT_ALIGNMENT,
    AlignmentLink,
    AlignmentRelation,
    SpaceTime,
    TraceData,
    create_api_app,
    create_explorer_app,
)
from spacetimepy.interface.web.service import TraceService

if TYPE_CHECKING:
    from pathlib import Path

    from spacetimepy import OfflineAlignmentContext


class _WebAlignment:
    name = "web-test"
    version = "1"

    def align(
        self,
        context: OfflineAlignmentContext,
    ) -> tuple[AlignmentLink, ...]:
        return (
            AlignmentLink(
                context.reference_steps[0],
                context.target_steps[0],
                AlignmentRelation.UPDATED,
            ),
        )


def create_trace(database: Path) -> dict[str, int]:
    space = SpaceTime.open(database, profile_capture=True)

    @space.capture.line
    def calculate(value: int) -> int:
        value += 1
        return value

    try:
        with space.capture.recording(
            mode="line",
            name="API example",
            description="Trace used by the explorer tests",
        ) as recording:
            calculate(4)
        root = space.data.get_branch(recording.branch_id)
        source = root.steps[0]
        replay = space.replay.run(
            lambda context: calculate(context.locals["value"] + 10),
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
            name="changed",
            configuration_key="changed-value",
        )
        return {
            "session": recording.session_id,
            "root_branch": recording.branch_id,
            "child_branch": replay.branch.id,
            "step": root.steps[0].id,
            "call": root.steps[0].stack_snapshot.function_call_id,
            "snapshot": root.steps[0].stack_snapshot.id,
        }
    finally:
        space.close()


def test_trace_data_offline_reader_lists_transport_neutral_entities(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_trace(database)

    with TraceData.open(database) as data:
        statistics = data.get_statistics()

        assert statistics.session_count == 1
        assert statistics.branch_count == 2
        assert statistics.step_count >= 2
        assert statistics.function_call_capture_performance_count == 2
        assert len(data.list_function_calls()) == 2
        assert len(data.list_function_call_performances()) == 2
        assert data.get_function_call_performance(identifiers["call"]) is not None
        assert data.list_stack_snapshots(identifiers["call"])
        assert data.list_code_definitions()
        assert data.list_stored_values()
        assert data.list_callee_calls(identifiers["call"]) == ()

    assert data.is_closed is True


def test_json_api_exposes_current_v2_exploration_without_comparison(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_trace(database)

    with TestClient(create_api_app(database)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["branch_count"] == 2
        assert health.json()["profiled_function_call_count"] == 2

        sessions = client.get("/api/sessions").json()["sessions"]
        assert sessions[0]["name"] == "API example"
        assert sessions[0]["branch_count"] == 2

        session = client.get(f"/api/session/{identifiers['session']}").json()
        assert {branch["id"] for branch in session["branches"]} == {
            identifiers["root_branch"],
            identifiers["child_branch"],
        }

        branch = client.get(
            f"/api/branch/{identifiers['child_branch']}?resolve=true"
        ).json()
        assert branch["resolved"] is True
        assert branch["configuration_key"] == "changed-value"

        step = client.get(f"/api/step/{identifiers['step']}").json()
        assert step["stack_snapshot"]["id"] == identifiers["snapshot"]
        assert step["locals"]["value"]["value"] == 4

        definition_id = step["stack_snapshot"]["code_definition_id"]
        definition = client.get(f"/api/code-definitions/{definition_id}").json()[
            "code_definition"
        ]
        assert definition["id"] == definition_id
        assert "def calculate" in definition["code_content"]

        calls = client.get("/api/function-calls?function=calculate").json()
        assert calls["total"] == 2
        assert all(call["function"] == "calculate" for call in calls["function_calls"])
        assert all(
            call["capture_performance"] is not None
            for call in calls["function_calls"]
        )

        call = client.get(f"/api/function-call/{identifiers['call']}").json()
        assert call["function_call"]["locals"]["value"]["value"] == 4
        assert call["function_call"]["has_stack_recording"] is True
        performance = call["function_call"]["capture_performance"]
        assert performance["unit"] == "nanoseconds"
        assert performance["direct_capture_ns"] >= 0
        assert performance["inclusive_capture_ns"] >= performance["direct_capture_ns"]
        assert performance["line_event_count"] > 0

        performance_endpoint = client.get(
            f"/api/function-call/{identifiers['call']}/capture-performance"
        )
        assert performance_endpoint.status_code == 200
        assert (
            performance_endpoint.json()["capture_performance"] == performance
        )

        stack = client.get(f"/api/stack-recording/{identifiers['call']}").json()
        assert stack["frames"]
        assert stack["code_definitions"]

        snapshot = client.get(f"/api/snapshot/{identifiers['snapshot']}").json()
        assert snapshot["snapshot"]["line"] > 0

        tree = client.get(
            f"/api/function-call/{identifiers['call']}/execution-tree"
        ).json()
        assert tree["execution_tree"]["function"] == "calculate"

        parts = client.get(
            f"/api/function-call/{identifiers['call']}/trace-parts"
        ).json()
        assert parts["parts"]
        assert sum(part["frame_count"] for part in parts["parts"]) == len(
            stack["frames"]
        )

        graph = client.get("/api/object-graph").json()
        assert f"session:{identifiers['session']}" in graph["nodes"]
        assert graph["edges"]

        assert client.get("/api/alignment/algorithms").json() == {
            "algorithms": [
                {
                    "name": STACK_SNAPSHOT_ALIGNMENT,
                    "version": "1",
                    "offline": True,
                    "online": False,
                }
            ]
        }
        default_alignment = client.post(
            "/api/alignment/compare",
            json={
                "reference_branch_id": identifiers["root_branch"],
                "target_branch_id": identifiers["child_branch"],
            },
        )
        assert default_alignment.status_code == 200
        assert (
            default_alignment.json()["alignment"]["algorithm"]
            == STACK_SNAPSHOT_ALIGNMENT
        )
        missing_alignment = client.post(
            "/api/alignment/compare",
            json={
                "reference_branch_id": identifiers["root_branch"],
                "target_branch_id": identifiers["child_branch"],
                "algorithm": "missing",
            },
        )
        assert missing_alignment.status_code == 404
        assert client.post("/api/refresh").json() == {"refreshed": True}
        assert client.post("/api/compare-traces").status_code == 404
        assert "/api/compare-traces" not in client.get("/openapi.json").json()["paths"]


def test_live_runtime_exposes_registered_alignment_on_demand() -> None:
    space = SpaceTime.open()

    @space.capture.function
    def calculate(value: int) -> int:
        return value + 1

    try:
        with space.capture.recording() as recording:
            calculate(1)
        root = space.data.get_branch(recording.branch_id)
        replay = space.replay.run(
            lambda context: calculate(context.locals["value"] + 1),
            parent_branch_id=root.id,
            forked_from_step_id=root.steps[0].id,
        )
        space.alignment.register(
            "web-test",
            version="1",
            offline=_WebAlignment,
        )

        with TestClient(create_api_app(space)) as client:
            algorithms = client.get("/api/alignment/algorithms").json()
            assert algorithms == {
                "algorithms": [
                    {
                        "name": STACK_SNAPSHOT_ALIGNMENT,
                        "version": "1",
                        "offline": True,
                        "online": False,
                    },
                    {
                        "name": "web-test",
                        "version": "1",
                        "offline": True,
                        "online": False,
                    },
                ]
            }

            response = client.post(
                "/api/alignment/compare",
                json={
                    "reference_branch_id": root.id,
                    "target_branch_id": replay.branch.id,
                    "algorithm": "web-test",
                    "options": {},
                },
            )

        assert response.status_code == 200
        alignment = response.json()["alignment"]
        assert alignment["algorithm"] == "web-test"
        assert alignment["links"] == [
            {
                "reference_step_id": root.steps[0].id,
                "target_step_id": replay.branch.steps[0].id,
                "relation": "updated",
            }
        ]
    finally:
        space.close()


def test_browser_explorer_keeps_previous_views_but_omits_comparison(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_trace(database)

    with TestClient(create_explorer_app(database)) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "Function Calls" in index.text
        assert "capturePerformanceTable" in index.text
        assert "formatCaptureDuration" in index.text
        assert "Compare Traces" not in index.text

        assert client.get("/sessions").status_code == 200
        assert client.get(f"/session/{identifiers['session']}").status_code == 200
        session = client.get(f"/session/{identifiers['session']}")
        assert "Branch Comparison" in session.text
        assert 'id="comparison-reference"' in session.text
        assert 'class="comparison-code-grid"' in session.text
        assert 'class="alignment-graph"' in session.text
        assert 'class="trace-cross-link' in session.text
        assert "nearestTraceStep" in session.text
        assert "SpaceTimeCodeMirror.ReadOnlyCodeView" in session.text
        assert ".comparison-code-empty[hidden]" in session.text
        assert "/api/code-definitions/" in session.text
        code_mirror = client.get("/static/codemirror.js")
        assert code_mirror.status_code == 200
        assert "ReadOnlyCodeView" in code_mirror.text
        assert "clearHighlight" in code_mirror.text
        assert client.get("/stack-recordings").status_code == 200
        assert client.get(f"/stack-recording/{identifiers['call']}").status_code == 200
        function_call = client.get(f"/function-call/{identifiers['call']}")
        assert function_call.status_code == 200
        assert "Capture Performance" in function_call.text
        assert client.get("/graph").status_code == 200
        assert client.get("/compare-traces").status_code == 404


def test_concurrent_branch_payloads_serialize_database_access(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_trace(database)

    with TraceData.open(database) as data:
        service = TraceService(data)
        original = data.get_function_call
        start = Barrier(3)
        second_entered = Event()
        counter_lock = Lock()
        active_calls = 0
        maximum_active_calls = 0

        def tracked_get_function_call(call_id: int):
            nonlocal active_calls, maximum_active_calls
            with counter_lock:
                active_calls += 1
                maximum_active_calls = max(maximum_active_calls, active_calls)
                first = active_calls == 1
            if first:
                second_entered.wait(0.05)
            else:
                second_entered.set()
            try:
                return original(call_id)
            finally:
                with counter_lock:
                    active_calls -= 1

        data.get_function_call = tracked_get_function_call

        def load_branch(branch_id: int, *, resolve: bool = False):
            start.wait()
            return service.branch(branch_id, resolve=resolve)

        with ThreadPoolExecutor(max_workers=2) as executor:
            reference = executor.submit(
                load_branch,
                identifiers["root_branch"],
                resolve=True,
            )
            target = executor.submit(
                load_branch,
                identifiers["child_branch"],
            )
            start.wait()
            assert reference.result()["id"] == identifiers["root_branch"]
            assert target.result()["id"] == identifiers["child_branch"]

        assert maximum_active_calls == 1
