from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from spacetimepy import AgentTraceService, SpaceTime, TraceData, create_mcp_server
from spacetimepy.interface.mcp.capture_guide import (
    CAPTURE_GUIDE,
    prepare_capture_prompt,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.client.session import ClientSession


def create_agent_trace(database: Path) -> dict[str, int | str]:
    space = SpaceTime.open(database)

    @space.capture.support
    def double(value: int) -> int:
        return value * 2

    @space.capture.line
    def calculate(value: int, payload: list[int]) -> int:
        value = double(value)
        value += len(payload)
        return value

    try:
        with space.capture.recording(
            mode="line",
            name="Agent example",
            description="Trace used by the MCP tests",
        ) as recording:
            calculate(4, [1, 2])
        root = space.data.get_branch(recording.branch_id)
        source = root.steps[0]
        call_id = source.stack_snapshot.function_call_id
        call = space.data.get_function_call(call_id)
        replay = space.replay.run(
            lambda context: calculate(
                context.locals["value"] + 10,
                context.locals["payload"],
            ),
            parent_branch_id=root.id,
            forked_from_step_id=source.id,
            name="changed",
            configuration_key="changed-value",
        )
        return {
            "session": recording.session_id,
            "root_branch": recording.branch_id,
            "child_branch": replay.branch.id,
            "step": source.id,
            "call": call_id,
            "definition": call.code_definition_id or "",
        }
    finally:
        space.close()


def test_agent_service_exposes_five_bounded_workflows(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_agent_trace(database)

    with TraceData.open(database) as data:
        service = AgentTraceService(data)

        overview = service.trace_overview()
        assert overview["data"]["read_only"] is True
        assert overview["data"]["replay_available"] is False
        assert overview["data"]["comparison_available"] is False
        assert overview["data"]["statistics"]["branches"] == 2
        assert overview["data"]["capture_guidance"] == {
            "resource": "spacetime://guides/capture",
            "prompt": "prepare_capture",
        }

        search = service.search_calls(query="calculate", limit=1)
        assert search["pagination"]["returned"] == 1
        assert search["pagination"]["truncated"] is True
        assert search["pagination"]["next_cursor"] == "1"
        second_page = service.search_calls(
            query="calculate",
            limit=1,
            cursor=search["pagination"]["next_cursor"],
        )
        assert second_page["pagination"]["returned"] == 1

        execution = service.execution_slice(
            branch_id=int(identifiers["root_branch"]),
            around_step_id=int(identifiers["step"]),
            radius=1,
        )
        assert execution["data"]["steps"][0]["resolved_position"] == 0
        assert execution["data"]["steps"][0]["recorded_position"] == 0

        def reject_dill_load(_data: bytes) -> Any:
            raise AssertionError("The safe default must not deserialize Dill data")

        data._serializer.loads = reject_dill_load
        step = service.inspect_step(int(identifiers["step"]))
        state = step["data"]["step"]["locals"]
        assert state["value"]["value"] == 4
        assert state["payload"]["is_primitive"] is False
        assert state["payload"]["value"].endswith("preview disabled>")
        assert step["data"]["step"]["source"]["resource_uri"].startswith(
            "spacetime://code/"
        )

        call = service.inspect_call(int(identifiers["call"]))
        assert call["data"]["call"]["function_name"] == "calculate"
        assert (
            call["data"]["call"]["callee_tree"]["children"][0]["function_name"]
            == "double"
        )
        assert call["data"]["call"]["snapshots"]


def test_agent_service_reports_actionable_bounds_and_missing_evidence(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_agent_trace(database)

    with TraceData.open(database) as data:
        service = AgentTraceService(data)
        no_match = service.search_calls(query="not-recorded")
        assert no_match["pagination"]["total"] == 0
        assert "recorded evidence" in no_match["warnings"][0]

        try:
            service.execution_slice(
                branch_id=int(identifiers["root_branch"]),
                start_position=999,
            )
        except ValueError as error:
            assert "outside branch range" in str(error)
        else:  # pragma: no cover - assertion clarity
            raise AssertionError("An invalid resolved position must fail")

        try:
            service.inspect_step(999_999)
        except Exception as error:  # noqa: BLE001 - public error type is asserted by text
            assert "execution step" in str(error)
        else:  # pragma: no cover - assertion clarity
            raise AssertionError("A missing step must fail")


def test_capture_guide_and_prompt_teach_current_public_interface() -> None:
    assert "@spacetimepy.function" in CAPTURE_GUIDE
    assert "@spacetimepy.line" in CAPTURE_GUIDE
    assert "@spacetimepy.support" in CAPTURE_GUIDE
    assert "@spacetimepy.external" in CAPTURE_GUIDE
    assert "space.capture.recording" in CAPTURE_GUIDE
    prompt = prepare_capture_prompt(
        objective="Find when score becomes negative",
        entrypoint="app.py",
    )
    assert "read-only trace exploration" in prompt
    assert "@spacetimepy.line" in prompt


def test_mcp_protocol_contract_has_only_agreed_capabilities(tmp_path) -> None:
    database = tmp_path / "trace.db"
    identifiers = create_agent_trace(database)

    async def exercise() -> None:
        server = create_mcp_server(database)
        async with create_connected_server_and_client_session(server) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "spacetime_trace_overview",
                "spacetime_search_calls",
                "spacetime_get_execution_slice",
                "spacetime_inspect_step",
                "spacetime_inspect_call",
            ]
            assert all(tool.outputSchema is not None for tool in tools.tools)
            assert all(tool.annotations.readOnlyHint for tool in tools.tools)
            assert all(tool.annotations.openWorldHint is False for tool in tools.tools)
            assert not any("replay" in tool.name for tool in tools.tools)
            assert not any("compare" in tool.name for tool in tools.tools)

            overview = await client.call_tool("spacetime_trace_overview", {})
            assert overview.isError is False
            assert overview.structuredContent["data"]["statistics"]["sessions"] == 1

            await _assert_tool_success(
                client,
                "spacetime_search_calls",
                {"query": "calculate"},
            )
            await _assert_tool_success(
                client,
                "spacetime_get_execution_slice",
                {"branch_id": identifiers["root_branch"]},
            )
            await _assert_tool_success(
                client,
                "spacetime_inspect_step",
                {"step_id": identifiers["step"]},
            )
            await _assert_tool_success(
                client,
                "spacetime_inspect_call",
                {"call_id": identifiers["call"]},
            )

            missing = await client.call_tool(
                "spacetime_inspect_step",
                {"step_id": 999_999},
            )
            assert missing.isError is True
            assert "execution step" in missing.content[0].text

            resources = await client.list_resources()
            assert {str(item.uri) for item in resources.resources} == {
                "spacetime://trace",
                "spacetime://guides/capture",
            }
            templates = await client.list_resource_templates()
            assert {item.uriTemplate for item in templates.resourceTemplates} == {
                "spacetime://sessions/{session_id}",
                "spacetime://branches/{branch_id}",
                "spacetime://steps/{step_id}",
                "spacetime://calls/{call_id}",
                "spacetime://code/{definition_id}",
            }
            assert not any(
                "value" in item.uriTemplate or "media" in item.uriTemplate
                for item in templates.resourceTemplates
            )

            guide = await client.read_resource("spacetime://guides/capture")
            assert "smallest useful capture boundary" in guide.contents[0].text
            session = await client.read_resource(
                f"spacetime://sessions/{identifiers['session']}"
            )
            assert json.loads(session.contents[0].text)["name"] == "Agent example"
            branch = await client.read_resource(
                f"spacetime://branches/{identifiers['root_branch']}"
            )
            assert json.loads(branch.contents[0].text)["resolved"] is True
            step = await client.read_resource(
                f"spacetime://steps/{identifiers['step']}"
            )
            assert json.loads(step.contents[0].text)["id"] == identifiers["step"]
            call = await client.read_resource(
                f"spacetime://calls/{identifiers['call']}"
            )
            assert json.loads(call.contents[0].text)["id"] == identifiers["call"]
            source = await client.read_resource(
                f"spacetime://code/{identifiers['definition']}"
            )
            assert "def calculate" in source.contents[0].text

            prompts = await client.list_prompts()
            assert [prompt.name for prompt in prompts.prompts] == ["prepare_capture"]
            prompt = await client.get_prompt(
                "prepare_capture",
                {"objective": "Find the invalid calculation"},
            )
            assert "smallest stable function boundary" in " ".join(
                prompt.messages[0].content.text.split()
            )

    anyio.run(exercise)


def test_mcp_borrows_an_existing_trace_reader(tmp_path) -> None:
    database = tmp_path / "trace.db"
    create_agent_trace(database)
    data = TraceData.open(database)

    async def exercise() -> None:
        server = create_mcp_server(data)
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("spacetime_trace_overview", {})

    try:
        anyio.run(exercise)
        assert data.is_closed is False
    finally:
        data.close()


def test_mcp_rejects_an_unauthenticated_remote_bind(tmp_path) -> None:
    database = tmp_path / "trace.db"
    create_agent_trace(database)

    try:
        create_mcp_server(database, host="0.0.0.0")
    except ValueError as error:
        assert "only binds to localhost" in str(error)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("The unauthenticated first version must remain local")


def test_mcp_creates_a_missing_trace_and_observes_the_first_capture(tmp_path) -> None:
    database = tmp_path / "first-trace.db"
    assert database.exists() is False

    server = create_mcp_server(database)
    assert database.is_file()

    async def exercise() -> None:
        async with create_connected_server_and_client_session(server) as client:
            empty = await client.call_tool("spacetime_trace_overview", {})
            assert empty.isError is False
            assert empty.structuredContent["data"]["statistics"]["sessions"] == 0
            empty_search = await client.call_tool(
                "spacetime_search_calls",
                {"query": "calculate"},
            )
            assert empty_search.structuredContent["pagination"]["total"] == 0

            with SpaceTime.open(database) as space:

                @space.capture.function
                def calculate(value: int) -> int:
                    return value + 1

                with space.capture.recording(name="first capture"):
                    calculate(4)

            populated = await client.call_tool("spacetime_trace_overview", {})
            assert populated.isError is False
            assert populated.structuredContent["data"]["statistics"]["sessions"] == 1
            populated_search = await client.call_tool(
                "spacetime_search_calls",
                {"query": "calculate"},
            )
            assert populated_search.structuredContent["pagination"]["total"] == 1

    anyio.run(exercise)


def test_trace_data_creation_is_opt_in_and_never_initializes_existing_files(
    tmp_path,
) -> None:
    missing = tmp_path / "missing.db"
    try:
        TraceData.open(missing)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("Ordinary trace readers still require an existing file")

    with TraceData.open(missing, create_if_missing=True) as data:
        assert data.get_statistics().session_count == 0
    assert missing.is_file()

    unrelated = tmp_path / "unrelated.db"
    unrelated.write_bytes(b"")
    try:
        TraceData.open(unrelated, create_if_missing=True)
    except Exception as error:  # noqa: BLE001 - public failure text is asserted
        assert "does not contain a SpaceTimePy v2 trace schema" in str(error)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("An existing non-trace file must never be initialized")


async def _assert_tool_success(
    client: ClientSession,
    name: str,
    arguments: dict[str, Any],
) -> None:
    result = await client.call_tool(name, arguments)
    assert result.isError is False
    assert result.structuredContent is not None
    assert set(result.structuredContent) == {
        "summary",
        "data",
        "resource_links",
        "pagination",
        "warnings",
    }
