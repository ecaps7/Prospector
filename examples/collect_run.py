#!/usr/bin/env python3
"""把一次已完成运行的全部产物收集到 examples/ 的运行目录里。

只是四条文档化 CLI 命令的包装：产物内容与手工执行它们完全一致，本脚本负责的是
落盘位置、覆盖保护、终端宽度，以及收尾时自动跑一遍血缘审计。

用法：
    python3 examples/collect_run.py <job-id> examples/<run-directory>/

前置条件：`prospector serve` 正在运行（`report export` 与 `job status` 走服务端），
且 .env 可用。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ("status.txt", "report.md", "report.json", "timeline.txt")


def _fail(message: str) -> int:
    print(f"失败：{message}", file=sys.stderr)
    return 1


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    start = next((i for i, part in enumerate(command) if part.startswith("prospector")), 0)
    # 进度写 stdout、错误写 stderr，重定向时两者按块缓冲会乱序，所以逐行冲刷。
    print(f"  $ {' '.join(command[start:])}", flush=True)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _report_failure(command: list[str], result: subprocess.CompletedProcess[str]) -> int:
    print(f"命令返回 {result.returncode}：{' '.join(command)}", file=sys.stderr)
    for stream_name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        text = stream.strip()
        if text:
            print(f"--- {stream_name} ---", file=sys.stderr)
            print(text, file=sys.stderr)
    return 1


def collect(job_id: UUID, directory: Path, env_file: str, columns: int) -> int:
    env = dict(os.environ, COLUMNS=str(columns))
    base = ["uv", "run", "--env-file", env_file]
    job = str(job_id)

    # job status 与 report export 走服务端 CLI；job events 走单进程入口，直接读 PostgreSQL。
    captured = (
        ("status.txt", [*base, "prospector", "job", "status", job]),
        ("timeline.txt", [*base, "prospector-local", "job", "events", job]),
    )
    exported = (
        ("report.md", [*base, "prospector", "report", "export", job, "--format", "md"]),
        ("report.json", [*base, "prospector", "report", "export", job, "--format", "json"]),
    )

    for name, command in captured:
        result = _run(command, env)
        if result.returncode != 0:
            return _report_failure(command, result)
        (directory / name).write_text(result.stdout, encoding="utf-8")

    for name, command in exported:
        # export 自己写文件，且拒绝覆盖已存在的目标——上面已经清过场。
        result = _run([*command, "--output", str(directory / name)], env)
        if result.returncode != 0:
            return _report_failure(command, result)

    print()
    for name in ARTIFACTS:
        size = (directory / name).stat().st_size
        print(f"  {directory / name}  {size:,} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="收集一次运行的 examples/ 产物并审计引用血缘。",
    )
    parser.add_argument("job_id", help="研究 Job 的 UUID")
    parser.add_argument("directory", type=Path, help="examples/ 下的运行目录")
    parser.add_argument("--env-file", default=".env", help="传给 uv run 的环境文件（默认 .env）")
    parser.add_argument("--columns", type=int, default=200, help="渲染表格的终端宽度")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的产物")
    parser.add_argument("--skip-audit", action="store_true", help="收集后不跑 verify_lineage.py")
    args = parser.parse_args(argv)

    try:
        job_id = UUID(args.job_id)
    except ValueError:
        return _fail(f"job_id 不是合法 UUID：{args.job_id}")

    directory: Path = args.directory
    if not directory.is_dir():
        return _fail(f"运行目录不存在：{directory}（先建目录并写好 question.md）")

    existing = [name for name in ARTIFACTS if (directory / name).exists()]
    if existing and not args.force:
        return _fail(f"产物已存在：{', '.join(existing)}；确认要重新收集就加 --force")
    for name in existing:
        (directory / name).unlink()

    print(f"收集 Job {job_id} 的产物到 {directory}", flush=True)
    code = collect(job_id, directory, args.env_file, args.columns)
    if code != 0 or args.skip_audit:
        return code

    print()
    audit = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("verify_lineage.py")), str(directory)],
        cwd=ROOT,
        check=False,
    )
    return audit.returncode


if __name__ == "__main__":
    sys.exit(main())
