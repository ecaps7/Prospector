"""Inspect emitted log records, not just the tracing API's current context."""

import json
import subprocess
import sys


def test_json_logs_contain_current_trace_and_job_without_polluting_stdout():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from opentelemetry import trace
from prospector.obs.logging import setup_logging, bind_job_id, get_logger
setup_logging(json_logs=True)
context = trace.SpanContext(trace_id=123, span_id=456, is_remote=False)
with trace.use_span(trace.NonRecordingSpan(context)):
    bind_job_id("test-job")
    get_logger("probe").info("inside")
bind_job_id(None)
get_logger("probe").info("outside")
""",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert probe.stdout == ""
    inside, outside = [json.loads(line) for line in probe.stderr.splitlines()]
    assert inside["event"] == "inside" and inside["job_id"] == "test-job"
    assert int(inside["trace_id"], 16) == 123 and int(inside["span_id"], 16) == 456
    assert outside["event"] == "outside"
    assert not {"job_id", "trace_id", "span_id"} & outside.keys()
