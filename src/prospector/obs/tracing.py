"""OpenTelemetry tracing — spans for log correlation; console export optional."""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

_provider: TracerProvider | None = None


def _console_enabled(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    raw = os.environ.get("PROSPECTOR_OTEL_CONSOLE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def setup_tracing(
    *,
    service_name: str = "prospector",
    console: bool | None = None,
) -> TracerProvider:
    """Initialize a process-wide TracerProvider. Idempotent.

    Spans are always created (so logs can inject trace_id / span_id).
    Pretty-printed ConsoleSpanExporter is opt-in via ``console=True`` or
    ``PROSPECTOR_OTEL_CONSOLE=1`` — otherwise CLI output is drowned in span dumps.
    """
    global _provider
    if _provider is not None:
        return _provider

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if _console_enabled(console):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer(name: str = "prospector") -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_context() -> tuple[str | None, str | None]:
    """Return (trace_id, span_id) hex strings for the active span, if any."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
