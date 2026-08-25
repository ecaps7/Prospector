import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { JobListItem } from "../api/types";

function statusDot(job: JobListItem): string {
  if (job.status === "completed") return job.outcome === "partial" ? "warn" : "ok";
  if (job.status === "failed") return "danger";
  if (job.status === "cancelled" || job.status === "cancelling") return "warn";
  return "dim";
}

function statusTag(job: JobListItem): { cls: string; label: string } {
  if (job.status === "completed") {
    if (job.outcome === "partial") return { cls: "warn", label: "partial" };
    if (job.outcome === "verified" || job.outcome === "draft_rendered") return { cls: "ok", label: job.outcome };
    return { cls: "ok", label: job.status };
  }
  if (job.status === "failed") return { cls: "danger", label: "failed" };
  if (job.status === "cancelled") return { cls: "warn", label: "cancelled" };
  if (job.status === "running") return { cls: "neutral", label: "running" };
  return { cls: "neutral", label: job.status };
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function JobsPage() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listJobs()
      .then((items) => {
        if (!cancelled) setJobs(items);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "无法加载任务列表");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="view">
      <div className="jobs-head">
        <h1>任务历史</h1>
        <span className="chip soft">本机单用户 · 一次只运行一个任务</span>
      </div>
      {error ? <p className="form-error">{error}</p> : null}
      <div className="card job-list">
        {jobs === null ? (
          <div className="jobs-empty">
            <span className="spinner" style={{ display: "inline-block", verticalAlign: "middle" }} /> 加载中…
          </div>
        ) : jobs.length === 0 ? (
          <div className="jobs-empty">还没有研究任务。从「发起新研究」提出一个问题即可。</div>
        ) : (
          jobs.map((job) => {
            const href =
              job.status === "completed" ? `/jobs/${job.job_id}/report` : `/jobs/${job.job_id}`;
            const tag = statusTag(job);
            return (
              <Link className="job-item" to={href} key={job.job_id}>
                <span className="st">
                  <span className={`dot ${statusDot(job)}`} />
                </span>
                <div className="main">
                  <div className="q">{job.question || "（未命名研究）"}</div>
                  <div className="sub">
                    <span className={`tag ${tag.cls}`}>{tag.label}</span>
                    {job.effort ? <span>{job.effort}</span> : null}
                    <span>{formatTime(job.created_at)}</span>
                  </div>
                </div>
                <span className="go">
                  {job.status === "completed" ? "查看报告" : "查看进度"} <span aria-hidden="true">›</span>
                </span>
              </Link>
            );
          })
        )}
      </div>
    </section>
  );
}
