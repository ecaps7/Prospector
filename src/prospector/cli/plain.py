"""Plain line-by-line projection of API job events."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

from prospector.cli.attach import AttachResult, run_attach
from prospector.cli.client import ProspectorClient
from prospector.runtime.timeline import emit_timeline_line


def attach_plain(
    client: ProspectorClient,
    job_id: UUID,
    *,
    report_root: Path | None = None,
) -> AttachResult:
    def emit_lines(_view: object, lines: list[str]) -> None:
        for line in lines:
            emit_timeline_line(line)

    return run_attach(
        client,
        job_id,
        report_root=report_root,
        on_update=emit_lines,
        on_reconnect=lambda _view, delay: print(
            f"SSE 已断开，{delay:g} 秒后重连…",
            file=sys.stderr,
            flush=True,
        ),
        on_connected=lambda _view: None,
    )


__all__ = ["AttachResult", "attach_plain"]
