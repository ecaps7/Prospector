import { Link } from "react-router-dom";
import type { JobListItem } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { StatusDot } from "../ui/StatusDot";
import { Tag, type TagTone } from "../ui/Tag";

function statusTag(job: JobListItem): { tone: TagTone; label: string } {
  if (job.status === "completed") {
    if (job.outcome === "partial") return { tone: "warn", label: "partial" };
    if (job.outcome === "verified" || job.outcome === "draft_rendered") {
      return { tone: "ok", label: job.outcome };
    }
    return { tone: "ok", label: job.status };
  }
  if (job.status === "failed") return { tone: "danger", label: "failed" };
  if (job.status === "cancelled") return { tone: "warn", label: "cancelled" };
  return { tone: "neutral", label: job.status };
}

export function JobItem({ job }: { job: JobListItem }) {
  const done = job.status === "completed";
  const href = done ? `/jobs/${job.job_id}/report` : `/jobs/${job.job_id}`;
  const tag = statusTag(job);
  return (
    <Link className="job-item" to={href}>
      <span className="st">
        <StatusDot status={job.status} outcome={job.outcome} />
      </span>
      <div className="main">
        <div className="q">{job.question || "（未命名研究）"}</div>
        <div className="sub">
          <Tag tone={tag.tone}>{tag.label}</Tag>
          {job.effort ? <span>{job.effort}</span> : null}
          <span>{fmtDateTime(job.created_at)}</span>
        </div>
      </div>
      <span className="go">
        {done ? "查看报告" : "查看进度"} <span aria-hidden="true">›</span>
      </span>
    </Link>
  );
}
