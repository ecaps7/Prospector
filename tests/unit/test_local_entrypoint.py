"""Unit tests for the single-process local entrypoint's job lifecycle handling."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import typer

from prospector.flow.research_graph import VerifierMajorGapError
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


def test_an_interrupted_local_run_leaves_the_job_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic escaped exception is an interruption, not a terminal state.

    Finalizing here would write job.stopped, and the finalize idempotency guard would
    then veto the completed write of a later successful resume -- stranding the job as
    'failed' even after the checkpoint re-entry rendered the report.
    """
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

    assert calls == []
    assert stopped == [True]


def test_verifier_major_gap_is_terminal_and_closes_the_job_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision budget is exhausted, so a resume would only hit the same wall."""
    calls: list[tuple[str, UUID, Any]] = []
    stopped: list[bool] = []
    _patch_graph(monkeypatch, calls, stopped, error=VerifierMajorGapError("major gap"))
    job_id = uuid4()

    with pytest.raises(VerifierMajorGapError, match="major gap"):
        local._run_research_graph(
            _FakeRepository(),  # type: ignore[arg-type]
            job_id,
            effort="quick",
            initial_state=None,
        )

    assert calls == [("failure", job_id, "verifier_major_gap")]
    assert stopped == [True]


def test_job_resume_refuses_a_job_that_already_reached_a_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job with job.stopped has no checkpoint left to re-enter: completed and cancelled
    jobs are done, and a budget-exhausted failure would only re-raise on resume."""

    class _Jobs:
        def stopped_event_id(self, job_id: UUID) -> int | None:
            return 42

    class _Research:
        def __init__(self) -> None:
            pass

    monkeypatch.setattr(local, "_bootstrap", lambda **kwargs: None)
    monkeypatch.setattr(local, "JobRepository", _Jobs)
    monkeypatch.setattr(local, "ResearchRepository", _Research)
    monkeypatch.setattr(local, "close_pool", lambda: None)

    with pytest.raises(typer.Exit) as exc_info:
        local.job_resume(str(uuid4()))

    assert exc_info.value.exit_code == 1
