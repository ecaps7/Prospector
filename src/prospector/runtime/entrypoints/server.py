"""Foreground entrypoint for the single-process Prospector API server."""

from __future__ import annotations

import socket
import subprocess
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


def _listeners_on_port(port: int) -> str | None:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    return "\n".join(lines)


def _ensure_port_free(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            typer.secho(
                f"端口 {port} 已被占用。先结束占用进程，或改用 "
                f"`prospector serve --port <其他端口>`。",
                fg=typer.colors.RED,
                err=True,
            )
            listeners = _listeners_on_port(port)
            if listeners is not None:
                typer.echo(listeners, err=True)
            else:
                typer.echo(
                    f"排查: lsof -nP -iTCP:{port} -sTCP:LISTEN",
                    err=True,
                )
            raise typer.Exit(code=1) from None


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

    host = "127.0.0.1"
    _ensure_port_free(host, port)
    try:
        uvicorn.run(
            create_app(services, validate_startup=False),
            host=host,
            port=port,
            workers=1,
        )
    finally:
        close_pool()
        clear_engine_cache()
