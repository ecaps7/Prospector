import { Link } from "react-router-dom";
import type { JobListItem } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { effortLabel, jobStatusLabel } from "../../lib/labels";
import { StatusDot } from "../ui/StatusDot";
import { Tag, type TagTone } from "../ui/Tag";

function statusTag(job: JobListItem): { tone: TagTone; label: string } {
  const label = jobStatusLabel(job.status, job.outcome);
  if (job.status === "completed") return { tone: job.outcome === "partial" ? "warn" : "ok", label };
  if (job.status === "failed") return { tone: "danger", label };
  if (job.status === "cancelled") return { tone: "warn", label };
  return { tone: "neutral", label };
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
          {job.effort ? <span>{effortLabel(job.effort)}</span> : null}
          <span>{fmtDateTime(job.created_at)}</span>
        </div>
      </div>
      <span className="go">
        {done ? "查看报告" : "查看进度"} <span aria-hidden="true">›</span>
      </span>
    </Link>
  );
}
