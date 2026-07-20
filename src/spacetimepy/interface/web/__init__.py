"""HTTP API and browser explorer adapters for the public SpaceTime interface."""

from .api import (
    ApiServerHandle,
    create_api_app,
    run_api,
    start_api,
)
from .service import TraceService


def create_explorer_app(*args, **kwargs):
    from .explorer import create_explorer_app as create

    return create(*args, **kwargs)


def run_explorer(*args, **kwargs):
    from .explorer import run_explorer as run

    return run(*args, **kwargs)

__all__ = [
    "ApiServerHandle",
    "TraceService",
    "create_api_app",
    "create_explorer_app",
    "run_api",
    "run_explorer",
    "start_api",
]
