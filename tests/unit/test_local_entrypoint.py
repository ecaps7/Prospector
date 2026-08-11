"""Unit tests for the single-process local entrypoint's job lifecycle handling."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from prospector.runtime.entrypoints import local


class _FakeRepository:
    def latest_event_id(self, job_id: UUID) -> int:
        return 0


def _patch_graph(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, UUID, Any]],
    stopped: list[bool],
    *,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    class _Graph:
        def invoke(self, state: Any, config: Any) -> dict[str, Any]:
            if error is not None:
                raise error
            assert result is not None
            return result

    class _Session:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *exc_info: object) -> bool:
            return False

    class _Follower:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            stopped.append(True)

    class _Jobs:
        def finalize_success(self, job_id: UUID, result: Any) -> None:
            calls.append(("success", job_id, result))

        def finalize_failure(self, job_id: UUID, *, fallback_error_code: str) -> None:
            calls.append(("failure", job_id, fallback_error_code))

    monkeypatch.setattr(local, "checkpointer_session", lambda: _Session())
    monkeypatch.setattr(local, "build_research_graph", lambda checkpointer: _Graph())
    monkeypatch.setattr(local, "ResearchTimelineRenderer", lambda *a, **k: object())
    monkeypatch.setattr(local, "ResearchTimelineFollower", _Follower)
    monkeypatch.setattr(local, "JobRepository", _Jobs)


def test_a_finished_local_run_closes_the_job_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing else can close it.

    ``create_job`` writes 'running' and ``set_research_outcome``'s status clause only ever
    writes 'failed', so without this call a completed local run keeps claiming to be running.
    """
    calls: list[tuple[str, UUID, Any]] = []
    stopped: list[bool] = []
    result = {"outcome": "draft_rendered", "phase": "draft_rendered"}
    _patch_graph(monkeypatch, calls, stopped, result=result)
    job_id = uuid4()

    local._run_research_graph(
        _FakeRepository(),  # type: ignore[arg-type]
        job_id,
        effort="quick",
        initial_state=None,
    )

    assert calls == [("success", job_id, result)]
    assert stopped == [True]


def test_a_failed_local_run_closes_the_job_row_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller still sees the failure; the row stops claiming the run is in flight."""
    calls: list[tuple[str, UUID, Any]] = []
    stopped: list[bool] = []
    _patch_graph(monkeypatch, calls, stopped, error=RuntimeError("graph exploded"))
    job_id = uuid4()

    with pytest.raises(RuntimeError, match="graph exploded"):
        local._run_research_graph(
            _FakeRepository(),  # type: ignore[arg-type]
            job_id,
            effort="quick",
            initial_state=None,
        )

    assert calls == [("failure", job_id, "job_execution_error")]
    assert stopped == [True]
