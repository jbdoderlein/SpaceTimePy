"""FastAPI transport over the public SpaceTime trace-data interface."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from spacetimepy.interface.alignment import (
    AlignmentAlgorithmNotFoundError,
    AlignmentError,
    AlignmentService,
)
from spacetimepy.interface.data import (
    TraceData,
    TraceDataError,
    TraceNotFoundError,
)
from spacetimepy.interface.runtime import SpaceTime
from spacetimepy.interface.web.service import TraceService

if TYPE_CHECKING:
    from collections.abc import Iterable

    from spacetimepy.core.serialization import CustomPickler


type TraceSource = TraceData | SpaceTime | str | Path


class AlignmentComparisonRequest(BaseModel):
    """One transient branch-alignment request."""

    reference_branch_id: int
    target_branch_id: int
    algorithm: str | None = Field(default=None, min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


def _open_source(
    source: TraceSource,
    custom_picklers: Iterable[CustomPickler],
) -> tuple[TraceData, AlignmentService, bool]:
    if isinstance(source, SpaceTime):
        if source.is_closed:
            raise RuntimeError("The supplied SpaceTime runtime is closed")
        return source.data, source.alignment, False
    if isinstance(source, TraceData):
        if source.is_closed:
            raise RuntimeError("The supplied trace-data reader is closed")
        return source, AlignmentService(source), False
    data = TraceData.open(source, custom_picklers=custom_picklers)
    return data, AlignmentService(data), True


def create_api_app(
    source: TraceSource,
    *,
    custom_picklers: Iterable[CustomPickler] = (),
) -> FastAPI:
    """Create the pure JSON API for a database, reader, or live runtime."""

    data, alignment, owns_data = _open_source(source, custom_picklers)
    service = TraceService(data, alignment)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        del application
        try:
            yield
        finally:
            if owns_data:
                data.close()

    application = FastAPI(
        title="SpaceTimePy API",
        description=(
            "Session, branch, step, VM-state, and object exploration over the "
            "SpaceTimePy public programmatic interface."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )
    application.state.trace_service = service
    application.state.alignment_service = alignment
    application.state.owns_trace_data = owns_data
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @application.exception_handler(TraceNotFoundError)
    async def trace_not_found(
        request: Request,
        error: TraceNotFoundError,
    ) -> Any:
        del request
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.exception_handler(TraceDataError)
    async def trace_data_error(request: Request, error: TraceDataError) -> Any:
        del request
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.exception_handler(AlignmentAlgorithmNotFoundError)
    async def alignment_algorithm_not_found(
        request: Request,
        error: AlignmentAlgorithmNotFoundError,
    ) -> Any:
        del request
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.exception_handler(AlignmentError)
    async def alignment_error(request: Request, error: AlignmentError) -> Any:
        del request
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", **service.database_info()}

    @application.get("/api/db-info")
    def database_info() -> dict[str, Any]:
        return service.database_info()

    @application.get("/api/sessions")
    def sessions() -> dict[str, Any]:
        return service.sessions()

    @application.get("/api/session/{session_id}")
    def session(session_id: int) -> dict[str, Any]:
        return service.session(session_id)

    @application.get("/api/branches/{branch_id}")
    @application.get("/api/branch/{branch_id}", include_in_schema=False)
    def branch(branch_id: int, resolve: bool = False) -> dict[str, Any]:
        return service.branch(branch_id, resolve=resolve)

    @application.get("/api/steps/{step_id}")
    @application.get("/api/step/{step_id}", include_in_schema=False)
    def step(step_id: int) -> dict[str, Any]:
        return service.step(step_id)

    @application.get("/api/code-definitions/{definition_id}")
    def code_definition(definition_id: str) -> dict[str, Any]:
        return service.code_definition(definition_id)

    @application.get("/api/alignment/algorithms")
    def alignment_algorithms() -> dict[str, Any]:
        return service.alignment_algorithms()

    @application.post("/api/alignment/compare")
    def compare_alignment(
        comparison: AlignmentComparisonRequest,
    ) -> dict[str, Any]:
        return service.compare_alignment(
            reference_branch_id=comparison.reference_branch_id,
            target_branch_id=comparison.target_branch_id,
            algorithm=comparison.algorithm,
            options=comparison.options,
        )

    @application.get("/api/function-calls")
    def function_calls(
        search: str = "",
        file: str = "",
        function: str = "",
        session_id: int | None = None,
        branch_id: int | None = None,
        limit: Annotated[int, Query(ge=1, le=5_000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return service.function_calls(
            search=search,
            file=file,
            function=function,
            session_id=session_id,
            branch_id=branch_id,
            limit=limit,
            offset=offset,
        )

    @application.get("/api/function-call/{call_id}")
    def function_call(call_id: int) -> dict[str, Any]:
        return service.function_call(call_id)

    @application.get("/api/function-call/{call_id}/capture-performance")
    def function_call_performance(call_id: int) -> dict[str, Any]:
        return service.function_call_performance(call_id)

    @application.get("/api/stack-recording/{call_id}")
    def stack_recording(call_id: int) -> dict[str, Any]:
        return service.stack_recording(call_id)

    @application.get("/api/snapshot/{snapshot_id}")
    def snapshot(snapshot_id: int) -> dict[str, Any]:
        return service.snapshot(snapshot_id)

    @application.get("/api/function-call/{call_id}/execution-tree")
    def execution_tree(
        call_id: int,
        max_depth: Annotated[int, Query(ge=0, le=50)] = 5,
    ) -> dict[str, Any]:
        return service.execution_tree(call_id, max_depth=max_depth)

    @application.get("/api/function-call/{call_id}/trace-parts")
    def trace_parts(call_id: int) -> dict[str, Any]:
        return service.trace_parts(call_id)

    @application.get("/api/function-call/{call_id}/graph")
    def function_graph(call_id: int) -> dict[str, Any]:
        return service.function_graph(call_id)

    @application.get("/api/object-graph")
    def object_graph(
        show_isolated: bool = False,
        max_nodes: Annotated[int, Query(ge=100, le=20_000)] = 2_000,
    ) -> dict[str, Any]:
        return service.object_graph(
            show_isolated=show_isolated,
            max_nodes=max_nodes,
        )

    @application.post("/api/refresh")
    def refresh() -> dict[str, bool]:
        return service.refresh()

    return application


def run_api(
    source: TraceSource,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    custom_picklers: Iterable[CustomPickler] = (),
) -> None:
    """Run the JSON API until interrupted."""

    import uvicorn

    uvicorn.run(
        create_api_app(source, custom_picklers=custom_picklers),
        host=host,
        port=port,
    )


@dataclass(frozen=True, slots=True)
class ApiServerHandle:
    """Background API server with an explicit cooperative stop operation."""

    thread: threading.Thread
    server: Any

    @property
    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def stop(self, timeout: float | None = 5.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


def start_api(
    source: TraceSource,
    port: int = 8000,
    host: str = "127.0.0.1",
    *,
    custom_picklers: Iterable[CustomPickler] = (),
) -> ApiServerHandle:
    """Start the API in a daemon thread for an in-process integration."""

    import uvicorn

    configuration = uvicorn.Config(
        create_api_app(source, custom_picklers=custom_picklers),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(configuration)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return ApiServerHandle(thread=thread, server=server)


__all__ = [
    "AlignmentComparisonRequest",
    "ApiServerHandle",
    "TraceSource",
    "create_api_app",
    "run_api",
    "start_api",
]
