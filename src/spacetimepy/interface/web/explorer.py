"""Combined SpaceTime JSON API and browser explorer command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from spacetimepy.interface.web.api import TraceSource, create_api_app
from spacetimepy.interface.web.ui import register_ui_routes

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from fastapi import FastAPI

    from spacetimepy.core.serialization import CustomPickler


def create_explorer_app(
    source: TraceSource,
    *,
    custom_picklers: Iterable[CustomPickler] = (),
) -> FastAPI:
    """Create one application serving both the JSON API and explorer pages."""

    application = create_api_app(source, custom_picklers=custom_picklers)
    register_ui_routes(application)
    return application


def run_explorer(
    source: TraceSource,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    api_only: bool = False,
    custom_picklers: Iterable[CustomPickler] = (),
) -> None:
    """Run the combined explorer, or only its JSON API."""

    import uvicorn

    application = (
        create_api_app(source, custom_picklers=custom_picklers)
        if api_only
        else create_explorer_app(source, custom_picklers=custom_picklers)
    )
    uvicorn.run(application, host=host, port=port)


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SpaceTimePy Web Explorer")
    parser.add_argument("database", type=Path, help="Existing SpaceTimePy v2 database")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Serve the JSON API without browser explorer pages",
    )
    options = parser.parse_args(arguments)
    run_explorer(
        options.database,
        host=options.host,
        port=options.port,
        api_only=options.api_only,
    )


if __name__ == "__main__":
    main()


__all__ = ["create_explorer_app", "main", "run_explorer"]
