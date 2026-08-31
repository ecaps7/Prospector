"""structlog console logging with OTel / job context injection.

All log output goes to **stderr** so that business output on stdout
(e.g. BRIEF_CONFIRMED, CLARIFY) stays clean and separable.
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.typing import EventDict

from prospector.obs.tracing import current_trace_context

_job_id_var: ContextVar[str | None] = ContextVar("prospector_job_id", default=None)
_configured = False

# Stage icons used in the event prefix — makes log lines scannable at a glance.
_STAGE_ICONS: dict[str, str] = {
    "bootstrap": "⚙",
    "scope.run": "·",
    "scope.clarify": "?",
    "scope.decision": "→",
    "scope.brief": "B",
    "brief.confirm": "✔",
    "llm.call": "~",
    "report_verifier.started": "…",
    "report_verifier.completed": "✓",
    "done": "✓",
    "error": "✗",
}

# ANSI colors (disabled when stderr is not a TTY, or NO_COLOR is set).
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"
_WHITE = "\033[37m"

_STAGE_COLORS: dict[str, str] = {
    "bootstrap": _DIM,
    "scope.run": _CYAN,
    "scope.clarify": _YELLOW,
    "scope.decision": _BOLD + _MAGENTA,
    "scope.brief": _BLUE,
    "brief.confirm": _GREEN,
    "llm.call": _DIM,
    "report_verifier.started": _CYAN,
    "report_verifier.completed": _GREEN,
    "done": _GREEN,
    "error": _RED,
}

_LEVEL_COLORS: dict[str, str] = {
    "debug": _DIM,
    "info": _WHITE,
    "warning": _YELLOW,
    "error": _RED,
    "critical": _BOLD + _RED,
}


def bind_job_id(job_id: str | None) -> None:
    _job_id_var.set(job_id)


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR", ""):
        return False
    if os.environ.get("FORCE_COLOR", ""):
        return True
    return sys.stderr.isatty()


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


def _suppress_noisy_loggers() -> None:
    """Silence HTTP client libraries — their INFO logs are pure noise in the CLI."""
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _format_kv(key: str, value: Any, *, color: bool) -> str:
    """Format a key=value pair; highlight decision-routing fields."""
    text = f"{key}={value}"
    if not color:
        return text
    if key == "next":
        if str(value) == "clarify":
            return f"{_BOLD}{_MAGENTA}{text}{_RESET}"
        if str(value) == "write_brief":
            return f"{_BOLD}{_GREEN}{text}{_RESET}"
    if key == "need_clarification":
        if value in (True, "True", "true"):
            return f"{_BOLD}{_MAGENTA}{text}{_RESET}"
        return f"{_GREEN}{text}{_RESET}"
    if key == "result" and str(value) in ("done", "ok"):
        return f"{_GREEN}{text}{_RESET}"
    return f"{_DIM}{text}{_RESET}"


class _ProspectorRenderer:
    """Compact, human-readable renderer with a stable ``[prospector]`` prefix.

    Example output (on stderr, colors omitted)::

        [prospector] ⚙ bootstrap      | config=loaded
        [prospector] ? scope.clarify  | question_len=10
        [prospector] → scope.decision | need_clarification=True  next=clarify
        [prospector] B scope.brief    | result=done  brief_len=1204
    """

    def __init__(self, *, colors: bool | None = None) -> None:
        self._colors = _colors_enabled() if colors is None else colors

    def __call__(self, _logger: Any, _method: str, event_dict: EventDict) -> str:
        event = str(event_dict.pop("event", ""))
        level = str(event_dict.pop("level", "info")).lower()
        # Remove internal keys we don't want to display
        for k in ("timestamp", "service", "trace_id", "span_id", "job_id"):
            event_dict.pop(k, None)

        # Pick icon: map event name to a known stage, fallback to level-based icon.
        icon = _STAGE_ICONS.get(event, "")
        if not icon:
            icon = {"debug": "·", "warning": "!", "error": "✗", "critical": "✗"}.get(level, "·")

        kvs = "  ".join(_format_kv(k, v, color=self._colors) for k, v in event_dict.items())
        prefix = f"[prospector] {icon} {event:<22s}"
        line = f"{prefix}| {kvs}" if kvs else prefix

        if not self._colors:
            return line

        color = _STAGE_COLORS.get(event) or _LEVEL_COLORS.get(level, _WHITE)
        # Color the prefix; keep already-styled KV highlights intact.
        if kvs:
            return f"{color}{prefix}{_RESET}| {kvs}"
        return f"{color}{line}{_RESET}"


def setup_logging(*, json_logs: bool = False) -> None:
    """Configure structlog + stdlib bridge. Idempotent.

    Defaults to human-readable console output on **stderr**.
    Set ``json_logs=True`` for structured JSON (e.g. in production).
    """
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
        renderer = _ProspectorRenderer()

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

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    _suppress_noisy_loggers()
    _configured = True


def get_logger(name: str = "prospector") -> structlog.stdlib.BoundLogger:
    setup_logging()
    return structlog.get_logger(name)
