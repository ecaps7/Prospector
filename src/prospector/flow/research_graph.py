"""M0 empty/probe research graph — checkpoint resume, not business logic."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from prospector.flow.state import EmptyFlowState
from prospector.obs.logging import get_logger

log = get_logger("prospector.flow")
tracer = trace.get_tracer("prospector.flow")


def _step_b_delay_seconds() -> float:
    raw = os.environ.get("PROSPECTOR_STEP_B_SLEEP_SECONDS", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _wait_flag_path() -> Path | None:
    raw = os.environ.get("PROSPECTOR_STEP_B_WAIT_FILE")
    if not raw:
        return None
    return Path(raw)


def _node(name: str, note: str):
    def _run(state: EmptyFlowState, config: RunnableConfig) -> dict[str, Any]:
        with tracer.start_as_current_span(f"empty_flow.{name}"):
            job_id = state["job_id"]
            next_step = int(state.get("step", 0)) + 1
            if name == "step_b":
                wait_path = _wait_flag_path()
                if wait_path is not None:
                    # Integration: block until flag file appears, then proceed.
                    # Process kill during wait leaves step_a checkpointed.
                    log.info(
                        "empty_flow_step_waiting",
                        message="step_b waiting for flag file",
                        job_id=job_id,
                        node=name,
                    )
                    while not wait_path.exists():
                        time.sleep(0.05)
                else:
                    delay = _step_b_delay_seconds()
                    if delay > 0:
                        log.info(
                            "empty_flow_step_waiting",
                            message=f"step_b sleeping {delay}s (kill window)",
                            job_id=job_id,
                            node=name,
                            sleep_seconds=delay,
                        )
                        time.sleep(delay)
            log.info(
                "empty_flow_step",
                message=f"{name} complete",
                job_id=job_id,
                step=next_step,
                node=name,
            )
            return {"step": next_step, "notes": [note]}

    _run.__name__ = name
    return _run


def build_empty_flow_graph(checkpointer: BaseCheckpointSaver) -> Any:
    graph = StateGraph(EmptyFlowState)
    graph.add_node("step_a", _node("step_a", "step_a"))
    graph.add_node("step_b", _node("step_b", "step_b"))
    graph.add_node("step_c", _node("step_c", "step_c"))
    graph.add_edge(START, "step_a")
    graph.add_edge("step_a", "step_b")
    graph.add_edge("step_b", "step_c")
    graph.add_edge("step_c", END)
    return graph.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}
