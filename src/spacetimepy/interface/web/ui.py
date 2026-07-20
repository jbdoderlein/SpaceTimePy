"""Server-rendered shell for the SpaceTime browser explorer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Request  # noqa: TC002 - FastAPI inspects this annotation
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from fastapi import FastAPI


_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def register_ui_routes(application: FastAPI) -> None:
    """Attach explorer pages that consume the app's public JSON endpoints."""

    def page(request: Request, template: str, **context: Any) -> Any:
        return _templates.TemplateResponse(
            request=request,
            name=template,
            context={"api_url": "", **context},
        )

    @application.get("/", include_in_schema=False)
    def index(request: Request) -> Any:
        return page(request, "index.html")

    @application.get("/function-call/{call_id}", include_in_schema=False)
    def function_call_page(request: Request, call_id: int) -> Any:
        return page(request, "function_call.html", call_id=call_id)

    @application.get("/stack-recordings", include_in_schema=False)
    def stack_recordings(request: Request) -> Any:
        return page(request, "stack_recordings.html")

    @application.get("/stack-recording/{call_id}", include_in_schema=False)
    def stack_recording(request: Request, call_id: int) -> Any:
        return page(request, "stack_recording.html", call_id=call_id)

    @application.get("/sessions", include_in_schema=False)
    def sessions(request: Request) -> Any:
        return page(request, "sessions.html")

    @application.get("/session/{session_id}", include_in_schema=False)
    def session(request: Request, session_id: int) -> Any:
        return page(request, "session_detail.html", session_id=session_id)

    @application.get("/graph", include_in_schema=False)
    def graph(request: Request) -> Any:
        return page(request, "graph.html")


__all__ = ["register_ui_routes"]
