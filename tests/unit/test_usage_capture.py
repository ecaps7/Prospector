from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from prospector.agents.usage import collect_usage, record_response_usage


class FakeRepository:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def record_usage(
        self,
        job_id: UUID,
        *,
        component: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        tool_calls: int = 0,
        task_id: UUID | None = None,
    ) -> None:
        self.rows.append(locals())


def test_provider_reported_usage_is_persisted_in_job_context() -> None:
    repository = FakeRepository()
    job_id = uuid4()
    task_id = uuid4()
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30))

    with collect_usage(
        repository,
        job_id,
        "research_worker",
        task_id=task_id,
    ):
        record_response_usage(response, "mid-model")

    assert len(repository.rows) == 1
    row = repository.rows[0]
    assert row["job_id"] == job_id
    assert row["task_id"] == task_id
    assert row["component"] == "research_worker"
    assert row["model"] == "mid-model"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 30


def test_missing_provider_usage_is_not_estimated() -> None:
    repository = FakeRepository()
    with collect_usage(repository, uuid4(), "planner"):
        record_response_usage(SimpleNamespace(usage=None), "model")
    assert repository.rows == []
