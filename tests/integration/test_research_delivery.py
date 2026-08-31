from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from functools import partial
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.support.providers import (
    DISPATCH,
    DOCUMENT,
    FACT,
    FINISH,
    PASS,
    QUALIFICATION,
    REVIEW,
    payload,
    verified_fact,
)

from prospector.agents.report_attribution import ClaimAttributionOutputError
from prospector.api.app import ApiServices, create_app
from prospector.api.scheduler import JobScheduler
from prospector.deterministic.markdown_report import partition_attribution_batches
from prospector.flow.cancellation import JobCancelledError
from prospector.flow.research_graph import build_research_graph, thread_config
from prospector.flow.state import initial_research_state
from prospector.schemas.brief import ResearchBrief
from prospector.store.checkpoint import get_checkpointer
from prospector.store.object_store import ObjectStore
from prospector.store.repositories.jobs import JobRepository
from prospector.store.repositories.research import ResearchRepository

pytestmark = pytest.mark.integration


def test_confirmed_brief_reaches_a_downloadable_cited_report(providers):
    providers.happy_path()
    jobs = JobRepository()
    ended = threading.Event()
    errors = []

    def execute(job_id, brief_id):
        def assert_phase_is_already_persisted(stage, _request):
            expected = {
                "synthesis": "synthesizing",
                "writer": "writing",
                "attribution": "attributing",
                "review": "reviewing",
            }
            if stage in expected:
                phases = [
                    e["payload"]["phase"]
                    for e in jobs.list_events_after(job_id, 0)
                    if e["event_type"] == "job.phase_changed"
                ]
                assert phases[-1] == expected[stage]

        providers.on_request = assert_phase_is_already_persisted
        try:
            return build_research_graph(get_checkpointer(), providers.services()).invoke(
                initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                thread_config(str(job_id)),
            )
        except Exception as exc:
            errors.append(exc)
            raise
        finally:
            ended.set()

    scheduler = JobScheduler(jobs, execute)
    services = ApiServices(jobs, ObjectStore(), scheduler, require_llm=lambda: None)
    with TestClient(create_app(services)) as client:
        created = client.post(
            "/api/jobs",
            json={
                "brief": ResearchBrief(
                    question="How did revenue change in 2024?",
                    brief_text="Use the annual report.",
                    effort="quick",
                ).model_dump(mode="json")
            },
        )
        assert created.status_code == 201
        job_id = created.json()["job_id"]
        assert ended.wait(20), "Research graph did not finish within the test deadline"
        if errors:
            raise errors[0]
        deadline = time.monotonic() + 5
        while jobs.stopped_event_id(UUID(job_id)) is None:
            assert time.monotonic() < deadline, "Scheduler did not finalize the graph result"
            time.sleep(0.01)
        events = client.get(f"/api/jobs/{job_id}/events")
        assert "event: job.stopped" in events.text
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["status"] == "completed", detail
        assert detail["report"]["verification_status"] == "verified"
        markdown = client.get(f"/api/jobs/{job_id}/report?format=md")
        audit = client.get(f"/api/jobs/{job_id}/report?format=json")
        assert markdown.status_code == audit.status_code == 200
        assert FACT in markdown.text and "[^1]" in markdown.text
        assert audit.json()["claim_evidence"]
        research = ResearchRepository()
        revision = research.get_markdown_revision(UUID(job_id))
        assert revision is not None
        with research.engine.connect() as conn:
            hashes = (
                conn.execute(
                    text(
                        "SELECT markdown_hash, json_hash FROM app.report_runs_v2 WHERE job_id=:job"
                    ),
                    {"job": UUID(job_id)},
                )
                .mappings()
                .one()
            )
        assert hashes["markdown_hash"] == "sha256:" + hashlib.sha256(markdown.content).hexdigest()
        assert hashes["json_hash"] == "sha256:" + hashlib.sha256(audit.content).hexdigest()
        evidence = audit.json()["claim_evidence"][0]
        assert FACT in json.dumps(evidence)
        # Full-page content is archived, not passed to any model or report consumer.
        prompts = json.dumps(dict(providers.requests))
        assert "ARCHIVE_ONLY_SENTINEL" not in prompts
        assert "ARCHIVE_ONLY_SENTINEL" not in markdown.text + audit.text
        assert (
            len(
                [
                    e
                    for e in jobs.list_events_after(UUID(job_id), 0)
                    if e["event_type"] == "report.draft_rendered"
                ]
            )
            == 1
        )
        document = research.find_document(
            "https://source.test/annual-report",
            "sha256:" + hashlib.sha256(DOCUMENT.encode()).hexdigest(),
        )
        assert document is not None
        key = document.storage_ref.split("/", 3)[3]
        assert ObjectStore().get_bytes(key).decode() == DOCUMENT
    providers.assert_consumed()


def test_format_errors_do_not_spend_the_verifiers_remaining_research_budget(providers):
    providers.happy_path()
    providers.responses["planner"].clear()
    providers.script("planner", "", "", *[FINISH] * 4, DISPATCH, FINISH, DISPATCH, FINISH)
    providers.worker_round()
    needs_research = {
        "decision": "needs_research",
        "reason": "An additional comparison is required.",
        "answerability_checks": [
            {
                "requirement": "Comparison",
                "status": "blocked",
                "answer": "",
                "supporting_assertion_refs": [],
                "evidence_bridge": "",
                "evidence_needed": "Comparable annual evidence",
            }
        ],
        "gaps": [
            {
                "kind": "plan_coverage",
                "severity": "major",
                "related_task_refs": ["t1"],
                "description": "Comparison missing",
                "evidence_needed": "Comparable annual evidence",
            }
        ],
    }
    providers.responses["verifier"].clear()
    providers.script("verifier", QUALIFICATION, needs_research, QUALIFICATION, PASS)
    job = create_job()
    result = run_graph(providers, job)
    assert result["research_decisions_used"] == 8
    assert result["decision_round"] == 10
    assert result["outcome"] == "report_rendered"
    qualification_prompts = [request["messages"] for request in providers.requests["verifier"][::2]]
    exits = [
        json.loads(prompt[-1]["content"].splitlines()[-1])["planner_exit"]
        for prompt in qualification_prompts
    ]
    assert [
        (exit["decision_round"], exit["research_decisions_used"], exit["decision_rounds_remaining"])
        for exit in exits
    ] == [(8, 6, 2), (10, 8, 0)]
    with ResearchRepository().engine.connect() as conn:
        persisted_prompts = list(
            conn.execute(
                text(
                    "SELECT full_prompt FROM app.verifier_runs "
                    "WHERE job_id=:job ORDER BY decision_round"
                ),
                {"job": job["job_id"]},
            ).scalars()
        )
    assert persisted_prompts == qualification_prompts
    replans = [
        event
        for event in ResearchRepository().list_events(job["job_id"])
        if event["event_type"] == "replan.triggered"
    ]
    assert len(replans) == 1
    providers.assert_consumed()


@pytest.mark.parametrize(
    "verdict,unchanged",
    [("partial", False), ("failed", False), ("failed", True)],
    ids=["partial", "failed", "unchanged-revision"],
)
def test_unqualified_report_is_delivered_without_confusing_job_lifecycle(
    providers, verdict, unchanged
):
    providers.happy_path()
    providers.responses["review"].clear()
    if verdict == "partial":

        def failed_fact(request):
            response = verified_fact(request)
            for claim in response["claims"]:
                claim.update(
                    status="failed",
                    excerpt_refs=[],
                    assertion_refs=[],
                    reason="The annual figure does not establish the claim's scope.",
                )
            return response

        providers.responses["attribution"].clear()
        providers.script("attribution", {"assertion_refs": ["a1"]}, failed_fact)
        providers.script("review", {"blocking_findings": [], "key_block_ids": ["b_0001"]})
    else:
        blocking_review = {
            "blocking_findings": [
                {
                    "kind": "conclusion_integrity",
                    "block_ids": ["b_0002"],
                    "reason": "The main conclusion does not follow from the annual change.",
                }
            ],
            "key_block_ids": ["b_0002"],
        }
        providers.script("review", blocking_review, blocking_review, blocking_review)
        for title in ("Revised", "Final"):
            replacements = (
                []
                if unchanged
                else [
                    {
                        "start_block_id": "b_0001",
                        "end_block_id": "b_0001",
                        "markdown": "# " + title,
                        "reason": "Clarify the summary heading.",
                    }
                ]
            )
            providers.script("writer", {"replacements": replacements})
    job = create_job()
    result = run_graph(providers, job)
    jobs = JobRepository()
    jobs.finalize_success(job["job_id"], result)
    detail = jobs.get_job(job["job_id"])
    assert detail is not None and detail["status"] == "completed"
    assert detail["verification_status"] == verdict
    app = create_app(ApiServices(jobs, ObjectStore(), JobScheduler(jobs, lambda *_: {})))
    with TestClient(app) as client:
        markdown = client.get(f"/api/jobs/{job['job_id']}/report?format=md")
        audit = client.get(f"/api/jobs/{job['job_id']}/report?format=json")
        assert markdown.status_code == audit.status_code == 200
        assert audit.json()["verification_status"] == verdict
        if verdict == "partial":
            assert "[^" not in markdown.text
            assert audit.json()["claim_evidence"] == []
        else:
            assert audit.json()["whole_report_review"]["blocking_findings"]
            last = ResearchRepository().get_markdown_revision(job["job_id"])
            assert last is not None and last["revision"] == 3
    providers.assert_consumed()


def create_job():
    return JobRepository().create_with_brief(
        ResearchBrief(
            question="How did revenue change in 2024?",
            brief_text="Use the annual report.",
            effort="quick",
        ),
        start_immediately=True,
    )


def run_graph(providers, job, *, resume=False):
    return build_research_graph(get_checkpointer(), providers.services()).invoke(
        None
        if resume
        else initial_research_state(job_id=str(job["job_id"]), brief_id=str(job["brief_id"])),
        thread_config(str(job["job_id"])),
    )


@pytest.mark.parametrize("committed", [False, True], ids=["files-only", "delivery-committed"])
def test_render_interruption_resumes_in_a_new_process_without_duplicate_delivery(
    providers,
    monkeypatch,
    committed,
):
    providers.happy_path()
    job = create_job()
    job_id = job["job_id"]
    original = ResearchRepository.complete_v2_report_render

    def interrupt(repository, *args, **kwargs):
        if committed:
            original(repository, *args, **kwargs)
        raise RuntimeError("injected interruption at the delivery boundary")

    with monkeypatch.context() as patch:
        patch.setattr(ResearchRepository, "complete_v2_report_render", interrupt)
        with pytest.raises(RuntimeError, match="injected interruption"):
            run_graph(providers, job)
    providers.assert_consumed()
    jobs = JobRepository()
    assert jobs.stopped_event_id(job_id) is None
    interrupted = jobs.get_job(job_id)
    assert interrupted is not None and interrupted["status"] == "running"
    store = ObjectStore()
    base = f"{ResearchRepository().settings.workspace_id}/reports/{job_id}/1"
    before = [store.get_bytes(base + suffix) for suffix in ("/report.md", "/report.json")]
    app = create_app(ApiServices(jobs, store, JobScheduler(jobs, lambda *_: {})))
    with TestClient(app) as client:
        assert client.get(f"/api/jobs/{job_id}/report").status_code == (200 if committed else 409)
    # No scripted answers in the child: any replayed model call fails immediately.
    child = subprocess.run(
        [sys.executable, "-m", "tests.support.resume", str(job_id)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    completed_job = jobs.get_job(job_id)
    assert completed_job is not None and completed_job["status"] == "completed"
    events = jobs.list_events_after(job_id, 0)
    assert len([e for e in events if e["event_type"] == "report.draft_rendered"]) == 1
    assert len([e for e in events if e["event_type"] == "job.stopped"]) == 1
    assert [store.get_bytes(base + suffix) for suffix in ("/report.md", "/report.json")] == before


def test_completed_attribution_batches_are_reused_by_report_identity(providers, monkeypatch):
    providers.happy_path()
    providers.responses["writer"].clear()
    providers.script("writer", FACT + "\n\n" + FACT)
    monkeypatch.setattr(
        "prospector.agents.report_attribution.partition_attribution_batches",
        partial(partition_attribution_batches, char_budget=500),
    )

    def broken_second_batch(request):
        body = payload(request)
        if "assertion_catalog" in body:
            return {"assertion_refs": ["a1"]}
        if body["candidates"][0]["block_id"] == "b_0002":
            return "invalid JSON"
        return verified_fact(request)

    providers.responses["attribution"].clear()
    providers.script("attribution", *[broken_second_batch] * 5)
    job = create_job()
    with pytest.raises(ClaimAttributionOutputError):
        run_graph(providers, job)
    repository = ResearchRepository()
    revision = repository.get_markdown_revision(job["job_id"])
    assert revision is not None
    attempt = repository.get_attribution_attempt(revision["report_id"], 1)
    assert attempt is not None
    before = repository.list_attribution_batches(attempt["id"])
    assert {row["status"] for row in before} == {"completed", "failed"}
    completed = next(row for row in before if row["status"] == "completed")
    first_call_count = len(providers.requests["attribution"])
    providers.script("attribution", verified_fact)
    result = run_graph(providers, job, resume=True)
    JobRepository().finalize_success(job["job_id"], result)
    retried = providers.requests["attribution"][first_call_count:]
    assert len(retried) == 1
    assert payload(retried[0])["candidates"][0]["block_id"] == "b_0002"
    after = repository.list_attribution_batches(attempt["id"])
    assert all(row["status"] == "completed" for row in after)
    assert next(row for row in after if row["batch_index"] == completed["batch_index"]) == completed
    providers.assert_consumed()


def test_readthrough_shares_two_repairs_and_reuses_unchanged_fact_attribution(providers):
    providers.happy_path()
    for title in ("Revised", "Final"):
        providers.script(
            "writer",
            {
                "replacements": [
                    {
                        "start_block_id": "b_0001",
                        "end_block_id": "b_0001",
                        "markdown": "# " + title,
                        "reason": "Correct the heading's summary.",
                    }
                ]
            },
        )
        providers.script("review", REVIEW)
    finding = {
        "findings": [
            {
                "kind": "summary_mismatch",
                "block_ids": ["b_0001"],
                "reason": "The heading does not describe the body.",
            }
        ]
    }
    providers.responses["readthrough"].clear()
    providers.script("readthrough", finding, finding, finding)
    job = create_job()
    result = run_graph(providers, job)
    repository = ResearchRepository()
    latest = repository.get_markdown_revision(job["job_id"])
    assert latest is not None and latest["revision"] == 3
    assert latest["markdown"] == "# Final\n\n" + FACT
    assert repository.get_markdown_revision(job["job_id"], revision=4) is None
    claims = []
    for number in (1, 2, 3):
        readthrough = repository.get_readthrough(latest["report_id"], number)
        assert readthrough is not None and readthrough["findings"]
        assert repository.get_report_review_run(latest["report_id"], number) is not None
        attribution = repository.get_attribution_run(latest["report_id"], number)
        assert attribution is not None and attribution.claim_evidence
        claims.append([claim.text for claim in attribution.claims])
    assert claims[0] == claims[1] == claims[2]
    assert result["outcome"] == "report_rendered"
    assert latest["verification_status"] == "verified"
    assert len(providers.requests["attribution"]) == 2
    providers.assert_consumed()


@pytest.mark.skip(reason="按用户要求暂缓：补研否决理由尚未传给 Writer，修复后恢复此测试")
def test_synthesis_request_must_be_admitted_by_verifier_before_more_research(providers):
    providers.happy_path()
    providers.responses["synthesis"].clear()
    providers.script(
        "synthesis",
        {
            "decision": "needs_research",
            "synthesis": "The annual figure establishes contraction.",
            "reason": "An additional comparison might help.",
            "evidence_needed": "A second market.",
            "assertion_refs": ["a1"],
            "material_conflict_refs": [],
        },
        {"defects": [], "reason": "Retain the limited analysis."},
    )
    providers.script("verifier", QUALIFICATION, PASS)
    job = create_job()
    result = run_graph(providers, job)
    assert result["outcome"] == "report_rendered"
    assert result["research_decisions_used"] == 2
    assert len(providers.requests["planner"]) == 2
    repository = ResearchRepository()
    with repository.engine.connect() as conn:
        triggers = set(
            conn.execute(
                text("SELECT trigger FROM app.verifier_runs WHERE job_id=:job"),
                {"job": job["job_id"]},
            ).scalars()
        )
    assert triggers == {"planner_finish", "synthesis_gap"}
    writer_input = payload(providers.requests["writer"][0])
    assert writer_input["research_synthesis"]["decision"] == "needs_research"
    assert writer_input["minor_gaps"]
    providers.assert_consumed()


def test_cancel_during_worker_call_prevents_further_evidence_and_report_writes(providers):
    providers.script("planner", DISPATCH)
    providers.script(
        "worker",
        {
            "action": "finish",
            "stop_reason": "no_public_evidence",
            "reason": "Stopped before finding evidence.",
        },
    )
    job = create_job()
    jobs = JobRepository()

    def request_cancellation(stage, _body):
        if stage == "worker":
            jobs.request_cancel(job["job_id"], requested_via="web_monitor")

    providers.on_request = request_cancellation
    with pytest.raises(JobCancelledError):
        run_graph(providers, job)
    jobs.finalize_cancelled(job["job_id"])
    stopped = jobs.get_job(job["job_id"])
    assert stopped is not None and stopped["status"] == "cancelled"
    assert ResearchRepository().count_excerpts(job["job_id"]) == 0
    assert ResearchRepository().get_markdown_revision(job["job_id"]) is None
    assert (
        len(
            [
                e
                for e in jobs.list_events_after(job["job_id"], 0)
                if e["event_type"] == "job.stopped"
            ]
        )
        == 1
    )
    providers.assert_consumed()


def test_unusable_assertion_stays_in_ledger_but_never_reaches_report_models(providers):
    providers.happy_path()
    unsupported = "UNUSABLE_ONLY_SENTINEL: future revenue is guaranteed."
    save = providers.responses["worker"][1]
    save["save_batches"][0]["findings"].append({"source_refs": ["s1:h1"], "statement": unsupported})

    def qualify(request):
        snapshot = json.loads(request["messages"][-1]["content"].splitlines()[-1])
        rejected = next(row for row in snapshot["assertions"] if row["statement"] == unsupported)
        return {
            **QUALIFICATION,
            "assertion_dispositions": [
                {
                    "assertion_ref": rejected["assertion_ref"],
                    "status": "unusable",
                    "reason": "The annual report does not establish future guarantees.",
                }
            ],
        }

    def coverage(request):
        snapshot = json.loads(request["messages"][-1]["content"].splitlines()[-1])
        usable = snapshot["usable_assertions"]
        assert len(usable) == 1 and usable[0]["statement"] == FACT
        return {
            **PASS,
            "answerability_checks": [
                {
                    **PASS["answerability_checks"][0],
                    "supporting_assertion_refs": [usable[0]["assertion_ref"]],
                }
            ],
        }

    providers.responses["verifier"].clear()
    providers.script("verifier", qualify, coverage)
    job = create_job()
    result = run_graph(providers, job)
    assert result["outcome"] == "report_rendered"
    repository = ResearchRepository()
    unusable = repository.list_effective_unusable_assertion_ids(job["job_id"])
    assert len(unusable) == 1
    snapshot = repository.build_writer_snapshot(job["job_id"], UUID(result["last_verifier_run_id"]))
    assert len(snapshot.evidence_cards) == 1
    assert not unusable & {card.assertion_id for card in snapshot.evidence_cards}
    for stage in ("synthesis", "writer", "attribution", "review"):
        assert unsupported not in json.dumps(providers.requests[stage])
    with repository.engine.connect() as conn:
        statements = set(
            conn.execute(
                text("SELECT statement FROM app.assertions WHERE job_id=:job"),
                {"job": job["job_id"]},
            ).scalars()
        )
    assert statements == {FACT, unsupported}
    providers.assert_consumed()
