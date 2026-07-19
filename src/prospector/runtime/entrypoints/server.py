"""Foreground entrypoint for the single-process Prospector API server."""

from __future__ import annotations

from typing import Annotated

import typer
import uvicorn

from prospector.api.app import create_app, default_services
from prospector.config import clear_settings_cache, get_settings
from prospector.obs.logging import setup_logging
from prospector.obs.tracing import setup_tracing
from prospector.store.checkpoint import close_pool, setup_checkpointer
from prospector.store.database import clear_engine_cache
from prospector.store.object_store import ObjectStore


def serve(
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 7620,
    initialize: Annotated[
        bool,
        typer.Option("--init", help="Create checkpointer tables and the MinIO bucket"),
    ] = False,
) -> None:
    """Run the local FastAPI service in one foreground process."""
    clear_settings_cache()
    setup_logging()
    setup_tracing()
    settings = get_settings()
    try:
        if initialize:
            setup_checkpointer(settings)
            ObjectStore(settings).ensure_bucket()
        services = default_services()
        services.repository.health_check()
        services.object_store.check_bucket()
    except Exception as exc:
        typer.secho(f"serve preflight failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    try:
        uvicorn.run(
            create_app(services, validate_startup=False),
            host="127.0.0.1",
            port=port,
            workers=1,
        )
    finally:
        close_pool()
        clear_engine_cache()
