from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from spacetimepy import SpaceTime, TraceData, create_api_app, create_explorer_app

if TYPE_CHECKING:
    from pathlib import Path


def create_trace(database: Path) -> dict[str, int]:
    space = SpaceTime.open(database)

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
        assert len(data.list_function_calls()) == 2
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

        calls = client.get("/api/function-calls?function=calculate").json()
        assert calls["total"] == 2
        assert all(call["function"] == "calculate" for call in calls["function_calls"])

        call = client.get(f"/api/function-call/{identifiers['call']}").json()
        assert call["function_call"]["locals"]["value"]["value"] == 4
        assert call["function_call"]["has_stack_recording"] is True

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

        assert client.post("/api/refresh").json() == {"refreshed": True}
        assert client.post("/api/compare-traces").status_code == 404
        assert "/api/compare-traces" not in client.get("/openapi.json").json()["paths"]


def test_browser_explorer_keeps_previous_views_but_omits_comparison(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_trace(database)

    with TestClient(create_explorer_app(database)) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "Function Calls" in index.text
        assert "Compare Traces" not in index.text

        assert client.get("/sessions").status_code == 200
        assert client.get(f"/session/{identifiers['session']}").status_code == 200
        assert client.get("/stack-recordings").status_code == 200
        assert (
            client.get(f"/stack-recording/{identifiers['call']}").status_code
            == 200
        )
        assert client.get("/graph").status_code == 200
        assert client.get("/compare-traces").status_code == 404
