"""Single-process local entrypoint: setup + interactive Scope with Brief HITL."""

from __future__ import annotations

import sys
import time
from typing import Annotated
from uuid import UUID

import typer

from prospector.agents.llm import LlmNotConfiguredError
from prospector.agents.scope import run_scope, write_research_brief
from prospector.config import clear_settings_cache, get_settings
from prospector.deterministic.budget import limits_for_effort
from prospector.flow.research_graph import build_research_graph, thread_config
from prospector.flow.state import initial_research_state
from prospector.obs.logging import get_logger, setup_logging
from prospector.obs.tracing import setup_tracing
from prospector.runtime.hitl.brief_confirm import (
    BriefConfirmAborted,
    confirm_brief,
    require_tty,
)
from prospector.runtime.timeline import (
    ResearchTimelineFollower,
    ResearchTimelineRenderer,
    emit_timeline_line,
    follow_timeline,
)
from prospector.schemas.brief import EffortLevel, ResearchBrief, ScopeOutcome
from prospector.store.checkpoint import checkpointer_session, close_pool, setup_checkpointer
from prospector.store.jobs import create_job
from prospector.store.object_store import ObjectStore
from prospector.store.repositories import ResearchRepository

app = typer.Typer(add_completion=False, no_args_is_help=True)
job_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(job_app, name="job")
log = get_logger("prospector.local")


def _output_separator() -> None:
    """Print a visual separator to stderr before business output on stdout.

    Intentionally ``"\\n" + "─" * 40`` — not ``"\\n─" * 40``, which would
    emit ~40 nearly-blank lines (one dash each).
    """
    print("\n" + "\u2500" * 40, file=sys.stderr)


def _bootstrap(*, require_llm: bool = False) -> None:
    clear_settings_cache()
    setup_logging()
    setup_tracing()
    settings = get_settings()  # fail-fast on missing DB / S3
    log.info("bootstrap", config="loaded", db=settings.database_url[:30] + "...")
    if require_llm:
        from prospector.agents.llm import require_llm_settings

        require_llm_settings(settings)
        log.info("bootstrap", llm="ready")
    log.info("bootstrap", result="done")


def format_scope_outcome(outcome: ScopeOutcome) -> str:
    if outcome.kind == "clarify":
        return f"CLARIFY:\n{outcome.clarification_question}"
    assert outcome.brief is not None
    brief = outcome.brief
    return (
        "BRIEF_PENDING:\n"
        f"question: {brief.question}\n"
        f"effort: {brief.effort}\n"
        f"language: {brief.language}\n"
        f"output_format: {brief.output_format}\n"
        f"\n{brief.brief_text}"
    )


def format_confirmed_brief(brief: ResearchBrief) -> str:
    return (
        "BRIEF_CONFIRMED:\n"
        f"question: {brief.question}\n"
        f"effort: {brief.effort}\n"
        f"language: {brief.language}\n"
        f"output_format: {brief.output_format}\n"
        f"\n{brief.brief_text}"
    )


@app.command()
def setup() -> None:
    """Create checkpointer tables and ensure the MinIO bucket exists."""
    _bootstrap()
    setup_checkpointer()
    store = ObjectStore()
    store.ensure_bucket()
    log.info("setup_complete", message="checkpointer + bucket ready")
    close_pool()


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Natural-language research question")],
    effort: Annotated[
        str,
        typer.Option("--effort", help="quick | standard | deep"),
    ] = "standard",
    language: Annotated[str, typer.Option("--language", help="Report language")] = "zh",
) -> None:
    """Confirm a Brief, then run the checkpointed Planner-Worker research loop."""
    if effort not in ("quick", "standard", "deep"):
        raise typer.BadParameter("effort must be quick, standard, or deep")
    effort_level: EffortLevel = effort  # type: ignore[assignment]
    t0 = time.monotonic()
    confirmed: ResearchBrief | None = None
    try:
        _bootstrap(require_llm=True)
        try:
            require_tty()
        except BriefConfirmAborted as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc

        outcome = run_scope(
            question,
            language=language,
            effort=effort_level,
        )
        if outcome.kind == "clarify":
            assert outcome.clarification_question is not None
            _output_separator()
            typer.echo(format_scope_outcome(outcome))
            clarification_answer = typer.prompt("回答").strip()
            if not clarification_answer:
                raise ValueError("clarification answer must not be blank")
            outcome = run_scope(
                question,
                clarification_question=outcome.clarification_question,
                clarification_answer=clarification_answer,
                language=language,
                effort=effort_level,
            )

        if outcome.kind != "brief_pending" or outcome.brief is None:
            raise RuntimeError(f"expected brief_pending, got {outcome.kind!r}")

        def revise_once(brief: ResearchBrief, note: str) -> ResearchBrief:
            return write_research_brief(
                question,
                previous_brief=brief,
                revision_note=note,
                language=language,
                effort=effort_level,
            )

        confirmed = confirm_brief(
            outcome.brief,
            prompt=lambda msg: typer.prompt(msg),
            revise_once_fn=revise_once,
            echo=lambda msg: typer.echo(msg, err=True),
        )
        log.info("brief.confirm", result="confirmed", brief_len=len(confirmed.brief_text))
        _output_separator()
        typer.echo(format_confirmed_brief(confirmed))

        job_id = create_job()
        repository = ResearchRepository()
        brief_id = repository.freeze_brief(job_id, confirmed)
        typer.echo(f"\nJOB_CREATED: {job_id}", err=True)
        typer.echo("研究时间线：", err=True)
        renderer = ResearchTimelineRenderer(
            repository,
            limits_for_effort(confirmed.effort),
        )
        follower = ResearchTimelineFollower(
            repository,
            renderer,
            job_id,
            after_id=repository.latest_event_id(job_id),
            emit=emit_timeline_line,
        )
        follower.start()
        try:
            with checkpointer_session() as checkpointer:
                graph = build_research_graph(checkpointer)
                result = graph.invoke(
                    initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                    thread_config(str(job_id)),
                )
        finally:
            follower.stop()
        typer.echo(
            f"RESEARCH_STOPPED: outcome={result['outcome']} phase={result['phase']}",
            err=True,
        )
    except BriefConfirmAborted as exc:
        log.info("brief.confirm", result="aborted", reason=str(exc))
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1) from exc
    except LlmNotConfiguredError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        log.exception("error", message=str(exc))
        typer.secho(f"scope failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        elapsed = time.monotonic() - t0
        log.info("done", elapsed=f"{elapsed:.1f}s")
        close_pool()

    assert confirmed is not None


@job_app.command("events")
def job_events(
    job_id: Annotated[str, typer.Argument(help="Research job UUID")],
    follow: Annotated[bool, typer.Option("--follow", help="Poll until research stops")] = False,
) -> None:
    """Render the append-only PostgreSQL research timeline."""
    try:
        parsed_job_id = UUID(job_id)
    except ValueError as exc:
        raise typer.BadParameter("job_id must be a UUID") from exc

    try:
        _bootstrap()
        repository = ResearchRepository()
        renderer = ResearchTimelineRenderer(
            repository,
            limits_for_effort(repository.get_job_effort(parsed_job_id)),
        )
        follow_timeline(
            repository,
            renderer,
            parsed_job_id,
            emit=emit_timeline_line,
            follow=follow,
        )
    finally:
        close_pool()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
