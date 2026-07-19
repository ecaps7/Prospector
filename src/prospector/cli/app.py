"""Root Typer application for the Prospector service and thin client."""

from __future__ import annotations

import sys
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never
from uuid import UUID

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from prospector.cli.attach import AttachResult
from prospector.cli.client import (
    CliApiError,
    CliConnectionError,
    CliLocalError,
    CliProtocolError,
    ProspectorClient,
)
from prospector.cli.plain import attach_plain
from prospector.cli.tui import attach_tui
from prospector.runtime.entrypoints.server import serve
from prospector.runtime.hitl.brief_confirm import (
    BriefConfirmAborted,
    confirm_brief,
    require_tty,
)
from prospector.schemas.brief import EffortLevel, ResearchBrief

app = typer.Typer(add_completion=False, invoke_without_command=True)
job_app = typer.Typer(add_completion=False, no_args_is_help=True)
report_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(job_app, name="job")
app.add_typer(report_app, name="report")
app.command()(serve)


class ReportFormat(StrEnum):
    MD = "md"
    JSON = "json"


def _fail(message: str, code: int) -> Never:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _handle_client_error(exc: Exception) -> Never:
    if isinstance(exc, CliApiError):
        code = (
            2
            if exc.status_code == 422
            or exc.error_code in {"job_not_found", "job_not_cancellable", "validation_error"}
            else 1
        )
        _fail(f"{exc.error_code}: {exc.message}", code)
    if isinstance(exc, CliConnectionError):
        _fail("服务端未运行，先执行: prospector serve", 1)
    if isinstance(exc, CliLocalError):
        _fail(str(exc), 1)
    _fail(f"服务端响应不符合契约: {exc}", 1)


def _print_created(job_id: UUID, status: str, queue_position: int | None) -> None:
    typer.echo(f"JOB_CREATED: {job_id}", err=True)
    if status == "queued":
        typer.echo(f"JOB_QUEUED: position={queue_position}", err=True)
    else:
        typer.echo("JOB_RUNNING", err=True)


def _print_stopped(result: AttachResult) -> None:
    typer.echo(
        f"RESEARCH_STOPPED: status={result.status} outcome={result.outcome} phase={result.phase}",
        err=True,
    )
    if result.report_path is not None:
        typer.echo(f"REPORT: {result.report_path}", err=True)


def _result_exit_code(result: AttachResult) -> int:
    if result.status in {"completed", "cancelled"}:
        return 0
    return 1 if result.error_code == "job_execution_error" else 3


def _print_plain_stopped(result: AttachResult) -> int:
    _print_stopped(result)
    return _result_exit_code(result)


def _print_rich_stopped(result: AttachResult) -> None:
    if result.status == "completed":
        title = "✔ 研究完成"
        style = "green"
    elif result.status == "cancelled":
        title = "⊘ 研究已取消"
        style = "yellow"
    else:
        title = "✘ 研究失败"
        style = "red"
    body = Text()
    body.append(f"状态    {result.outcome or result.status}\n")
    if result.report_path is not None:
        body.append(f"报告    {result.report_path}\n")
        body.append(f"查看    prospector report show {result.view.job_id}")
    elif result.error_code:
        body.append(f"错误    {result.error_code}")
    Console().print(Panel(body, title=title, border_style=style))


def _attach(client: ProspectorClient, job_id: UUID, *, plain: bool) -> int:
    use_plain = plain or not sys.stdout.isatty()
    try:
        result = attach_plain(client, job_id) if use_plain else attach_tui(client, job_id)
    except KeyboardInterrupt:
        typer.echo(
            f"已离开，任务继续运行：prospector job attach {job_id}",
            err=True,
        )
        return 0
    if use_plain:
        return _print_plain_stopped(result)
    _print_rich_stopped(result)
    return _result_exit_code(result)


def _research_once(
    client: ProspectorClient,
    question: str,
    *,
    effort: EffortLevel,
    language: str,
    plain: bool,
) -> int:
    created_job_id: UUID | None = None
    try:
        typer.echo("Scope 正在展开问题…", err=True)
        outcome = client.scope(question, effort=effort, language=language)
        if outcome.kind == "clarify":
            if outcome.clarification_question is None:
                raise CliProtocolError("Clarification response is missing its question")
            typer.echo(f"CLARIFY: {outcome.clarification_question}", err=True)
            answer = typer.prompt("回答").strip()
            if not answer:
                typer.secho("澄清回答不能为空", fg=typer.colors.RED, err=True)
                return 2
            outcome = client.scope(
                question,
                effort=effort,
                language=language,
                clarification_question=outcome.clarification_question,
                clarification_answer=answer,
            )
        if outcome.kind != "brief_pending" or outcome.brief is None:
            raise CliProtocolError("Scope did not produce a Brief after clarification")

        def revise_once(brief: ResearchBrief, note: str) -> ResearchBrief:
            return client.revise_scope(
                question,
                brief,
                note,
                effort=effort,
                language=language,
            )

        try:
            brief = confirm_brief(
                outcome.brief,
                prompt=lambda message: typer.prompt(message),
                revise_once_fn=revise_once,
                echo=lambda message: typer.echo(message, err=True),
            )
        except BriefConfirmAborted as exc:
            if exc.reason == "user_aborted":
                typer.echo(str(exc), err=True)
                return 0
            raise

        created = client.create_job(brief)
        created_job_id = created.job_id
        _print_created(created.job_id, created.status, created.queue_position)
        return _attach(client, created.job_id, plain=plain)
    except (KeyboardInterrupt, typer.Abort):
        if created_job_id is not None:
            typer.echo(
                f"已离开，任务继续运行：prospector job attach {created_job_id}",
                err=True,
            )
            return 0
        typer.echo("已取消本次研究，返回问题输入。", err=True)
        return 130


@app.callback()
def main(
    context: typer.Context,
    effort: Annotated[
        EffortLevel,
        typer.Option("--effort", help="Default effort for this interactive session"),
    ] = "standard",
    language: Annotated[
        str,
        typer.Option("--language", help="Default report language for this session"),
    ] = "zh",
    plain: Annotated[
        bool,
        typer.Option("--plain", help="Use line-by-line output instead of Rich Live"),
    ] = False,
) -> None:
    """Open the interactive Prospector research console."""
    if context.invoked_subcommand is not None:
        return
    try:
        require_tty()
    except BriefConfirmAborted as exc:
        _fail(str(exc), 2)

    try:
        with ProspectorClient() as client:
            client.health()
            Console().print(
                Panel(
                    f"服务已连接 · {effort} · {language}\n"
                    "输入研究问题开始；Ctrl-C 或 Ctrl-D 退出。",
                    title="Prospector",
                    border_style="cyan",
                )
            )
            while True:
                try:
                    question = typer.prompt("研究问题").strip()
                except (KeyboardInterrupt, EOFError, typer.Abort):
                    typer.echo("\n已退出 Prospector。", err=True)
                    return
                if not question:
                    typer.secho("研究问题不能为空", fg=typer.colors.RED, err=True)
                    continue
                try:
                    _research_once(
                        client,
                        question,
                        effort=effort,
                        language=language,
                        plain=plain,
                    )
                except BriefConfirmAborted as exc:
                    typer.secho(str(exc), fg=typer.colors.RED, err=True)
                except (
                    CliApiError,
                    CliConnectionError,
                    CliLocalError,
                    CliProtocolError,
                ) as exc:
                    with suppress(typer.Exit):
                        _handle_client_error(exc)
                typer.echo("", err=True)
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)


@job_app.command("attach")
def job_attach(
    job_id: Annotated[UUID, typer.Argument(help="Research job UUID")],
    plain: Annotated[
        bool,
        typer.Option("--plain", help="Render a line-by-line timeline"),
    ] = False,
) -> None:
    """Replay and follow a Job's persisted event stream."""
    try:
        with ProspectorClient() as client:
            client.health()
            exit_code = _attach(client, job_id, plain=plain)
            if exit_code:
                raise typer.Exit(code=exit_code)
    except KeyboardInterrupt:
        typer.echo(
            f"已离开，任务继续运行：prospector job attach {job_id}",
            err=True,
        )
        return
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)


@job_app.command("list")
def job_list() -> None:
    """List persisted research Jobs."""
    try:
        with ProspectorClient() as client:
            client.health()
            jobs = client.list_jobs()
        table = Table(title="Prospector Jobs", box=None)
        table.add_column("job_id", no_wrap=True)
        table.add_column("问题", overflow="ellipsis")
        table.add_column("effort")
        table.add_column("状态")
        table.add_column("阶段")
        table.add_column("创建时间", no_wrap=True)
        table.add_column("更新时间", no_wrap=True)
        for job in jobs:
            table.add_row(
                str(job.job_id),
                job.question or "-",
                job.effort or "-",
                job.status,
                job.phase,
                job.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                job.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            )
        console = Console()
        Console(width=max(160, console.width)).print(table)
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)


@job_app.command("cancel")
def job_cancel(
    job_id: Annotated[UUID, typer.Argument(help="Research job UUID")],
) -> None:
    """Cancel a queued or running Job."""
    try:
        with ProspectorClient() as client:
            client.health()
            result = client.cancel_job(job_id)
        if result.status == "cancelled":
            typer.echo(f"JOB_CANCELLED: {job_id}")
        else:
            typer.echo(f"CANCEL_REQUESTED: {job_id}")
            typer.echo("任务将在当前模型或工具调用结束后的安全边界停止。", err=True)
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)


@job_app.command("status")
def job_status(
    job_id: Annotated[UUID, typer.Argument(help="Research job UUID")],
) -> None:
    """Show the authoritative snapshot for one Job."""
    try:
        with ProspectorClient() as client:
            client.health()
            detail = client.get_job(job_id)
        console = Console()
        summary = Table.grid(padding=(0, 2))
        for label, value in (
            ("Job", str(detail.job_id)),
            ("问题", detail.question or "-"),
            ("状态", detail.status),
            ("阶段", detail.phase),
            ("Plan", f"v{detail.plan_version}"),
            ("结果", detail.outcome or "-"),
            ("错误", detail.error_code or "-"),
        ):
            summary.add_row(label, str(value))
        console.print(Panel(summary, title="Job 状态"))

        tasks = Table(title="任务", box=None)
        for column in ("task_id", "问题", "阶段", "模式", "状态", "工具调用"):
            tasks.add_column(column)
        for task in detail.tasks:
            tasks.add_row(
                str(task.task_id),
                task.question,
                task.research_stage,
                task.research_mode,
                task.status,
                str(task.tool_calls_used),
            )
        console.print(tasks)

        usage = Table(title="用量", box=None)
        for column in ("component", "input tokens", "output tokens", "tool calls"):
            usage.add_column(column)
        for item in detail.usage:
            usage.add_row(
                item.component,
                str(item.input_tokens),
                str(item.output_tokens),
                str(item.tool_calls),
            )
        if detail.usage:
            usage.add_section()
            usage.add_row(
                "TOTAL",
                str(sum(item.input_tokens for item in detail.usage)),
                str(sum(item.output_tokens for item in detail.usage)),
                str(sum(item.tool_calls for item in detail.usage)),
            )
        else:
            usage.add_row("暂无记录", "-", "-", "-")
        console.print(usage)

        report = detail.report
        console.print(
            f"报告：{report.status if report is not None else '尚未生成'}"
            + (f" · verification={report.verification_status or '-'}" if report is not None else "")
        )
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)


@report_app.command("show")
def report_show(
    job_id: Annotated[UUID, typer.Argument(help="Research job UUID")],
) -> None:
    """Render a Job's Markdown report in the terminal."""
    try:
        with ProspectorClient() as client:
            client.health()
            content = client.download_report(job_id, "md")
        try:
            markdown = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CliProtocolError("报告不是有效的 UTF-8 Markdown") from exc
        Console().print(Markdown(markdown))
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)


@report_app.command("export")
def report_export(
    job_id: Annotated[UUID, typer.Argument(help="Research job UUID")],
    report_format: Annotated[
        ReportFormat,
        typer.Option("--format", help="md | json"),
    ] = ReportFormat.MD,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Destination file"),
    ] = None,
) -> None:
    """Export the original Markdown or JSON report bytes."""
    destination = output or Path(f"report.{report_format.value}")
    if destination.exists():
        _fail(f"目标文件已存在：{destination}", 2)
    try:
        with ProspectorClient() as client:
            client.health()
            content = client.download_report(job_id, report_format.value)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as file:
                file.write(content)
        except FileExistsError:
            _fail(f"目标文件已存在：{destination}", 2)
        except OSError as exc:
            raise CliLocalError(f"无法写入报告：{destination}") from exc
        typer.echo(f"REPORT_EXPORTED: {destination.resolve()}")
    except (CliApiError, CliConnectionError, CliLocalError, CliProtocolError) as exc:
        _handle_client_error(exc)
