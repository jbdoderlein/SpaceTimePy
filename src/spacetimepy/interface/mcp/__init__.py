"""Agent-oriented SpaceTime trace exploration over MCP."""

from __future__ import annotations

from typing import Any

from .capture_guide import CAPTURE_GUIDE, prepare_capture_prompt
from .service import AgentResult, AgentTraceService


def create_mcp_server(*args: Any, **kwargs: Any) -> Any:
    """Lazily create the MCP server without importing its SDK during discovery."""

    from .server import create_mcp_server as create

    return create(*args, **kwargs)


def run_mcp(*args: Any, **kwargs: Any) -> None:
    """Lazily run the SpaceTime trace MCP."""

    from .server import run_mcp as run

    run(*args, **kwargs)


__all__ = [
    "CAPTURE_GUIDE",
    "AgentResult",
    "AgentTraceService",
    "create_mcp_server",
    "prepare_capture_prompt",
    "run_mcp",
]
