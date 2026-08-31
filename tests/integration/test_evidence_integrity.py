from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event
from tests.support.providers import FACT, SOURCE_URL

from prospector.schemas.brief import ResearchBrief
from prospector.schemas.plan import ResearchTask, TaskBudget
from prospector.store.repositories.jobs import JobRepository
from prospector.store.repositories.research import ResearchRepository
from prospector.tools.base import ToolContext
from prospector.tools.save_findings import SaveFindingsTool
from prospector.tools.web_fetch import WebFetchTool

pytestmark = pytest.mark.integration


@pytest.fixture
def task():
    created = JobRepository().create_with_brief(
        ResearchBrief(question="Revenue?", brief_text="Annual report"),
        start_immediately=True,
    )
    task = ResearchTask(
        task_id=uuid4(),
        question="Revenue?",
        expected_evidence="Annual report",
        budget=TaskBudget(max_worker_rounds=12),
    )
    ResearchRepository().create_plan(created["job_id"], 1, [task], reason="Collect evidence")
    return ToolContext(created["job_id"], task.task_id, "test-worker", task.question, "fetch")


def arguments(fetched):
    return {
        "doc_id": fetched["doc_id"],
        "view_id": fetched["view_id"],
        "findings": [{"statement": FACT, "source_ids": ["h1"], "topic_tags": []}],
    }


async def test_fetch_archives_but_only_selected_evidence_is_saved_and_repeated_saves_deduplicate(
    task,
    providers,
):
    repository = ResearchRepository()
    fetch = WebFetchTool(repository)
    fetched = await fetch({"url": SOURCE_URL}, task)
    assert repository.count_excerpts(task.job_id) == 0
    save = SaveFindingsTool(repository)
    first = await save(arguments(fetched), replace(task, tool_call_id="save-one"))
    second = await save(arguments(fetched), replace(task, tool_call_id="save-two"))
    assert first["inserted"] > 0 and second["inserted"] == 0
    assert first["assertion_ids"] == second["assertion_ids"]
    assert repository.count_excerpts(task.job_id) == 1
    assertions = repository.list_assertions(task.task_id)
    assert len(assertions) == 1 and assertions[0].statement == FACT
    again = await fetch({"url": SOURCE_URL}, replace(task, tool_call_id="fetch-again"))
    assert again["doc_id"] == fetched["doc_id"] and again["version"] == fetched["version"]
    providers.document += "\nChanged archived content."
    changed = await fetch({"url": SOURCE_URL}, replace(task, tool_call_id="fetch-new-version"))
    assert changed["doc_id"] != fetched["doc_id"] and changed["version"] == fetched["version"] + 1


@pytest.mark.parametrize("wrong", ["job", "task", "version", "source"])
async def test_save_rejects_evidence_outside_the_current_task_and_document_version(
    task,
    providers,
    wrong,
):
    repository = ResearchRepository()
    fetched = await WebFetchTool(repository)({"url": SOURCE_URL}, task)
    args = arguments(fetched)
    context = task
    if wrong == "job":
        context = replace(task, job_id=uuid4())
    elif wrong == "task":
        context = replace(task, task_id=uuid4())
    elif wrong == "source":
        args["findings"][0]["source_ids"] = ["h999"]
    else:
        providers.document += "\nNew version"
        newer = await WebFetchTool(repository)({"url": SOURCE_URL}, task)
        args["doc_id"] = newer["doc_id"]
    before = repository.list_events(task.job_id)
    reason = {
        "job": "does not belong",
        "task": "does not belong",
        "version": "does not match",
        "source": "source ids",
    }[wrong]
    with pytest.raises(ValueError, match=reason):
        await SaveFindingsTool(repository)(args, context)
    assert repository.count_excerpts(task.job_id) == 0
    assert repository.list_assertions(task.task_id) == []
    assert repository.list_events(task.job_id) == before


async def test_failed_assertion_insert_rolls_back_excerpt_and_evidence_event(task, providers):
    repository = ResearchRepository()
    fetched = await WebFetchTool(repository)({"url": SOURCE_URL}, task)
    before = repository.list_events(task.job_id)

    def reject_insert(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO app.assertions" in statement:
            raise RuntimeError("injected database failure")

    event.listen(repository.engine, "before_cursor_execute", reject_insert)
    try:
        with pytest.raises(RuntimeError, match="injected database failure"):
            await SaveFindingsTool(repository)(arguments(fetched), task)
    finally:
        event.remove(repository.engine, "before_cursor_execute", reject_insert)
    assert repository.count_excerpts(task.job_id) == 0
    assert repository.list_assertions(task.task_id) == []
    assert repository.list_events(task.job_id) == before


@pytest.mark.skip(reason="按用户要求暂缓：Excerpt 尚未核对归档原文，修复后恢复此测试")
async def test_highlight_absent_from_archived_document_cannot_become_an_excerpt(task, providers):
    repository = ResearchRepository()
    providers.highlights = ["Invented highlight absent from the saved document."]
    fetched = await WebFetchTool(repository)({"url": SOURCE_URL}, task)
    with pytest.raises(ValueError):
        await SaveFindingsTool(repository)(arguments(fetched), task)
    assert repository.count_excerpts(task.job_id) == 0
    assert repository.list_assertions(task.task_id) == []
