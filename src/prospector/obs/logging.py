"""structlog JSON logging with OTel / job context injection."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.typing import EventDict

from prospector.obs.tracing import current_trace_context

_job_id_var: ContextVar[str | None] = ContextVar("prospector_job_id", default=None)
_configured = False


def bind_job_id(job_id: str | None) -> None:
    _job_id_var.set(job_id)


def get_job_id() -> str | None:
    return _job_id_var.get()


def _inject_context(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    trace_id, span_id = current_trace_context()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    if span_id:
        event_dict.setdefault("span_id", span_id)
    job_id = _job_id_var.get()
    if job_id:
        event_dict.setdefault("job_id", job_id)
    event_dict.setdefault("service", "prospector")
    return event_dict


def setup_logging(*, json_logs: bool = True) -> None:
    """Configure structlog + stdlib bridge. Idempotent."""
    global _configured
    if _configured:
        return

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_logs:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _configured = True


def get_logger(name: str = "prospector") -> structlog.stdlib.BoundLogger:
    setup_logging()
    return structlog.get_logger(name)
