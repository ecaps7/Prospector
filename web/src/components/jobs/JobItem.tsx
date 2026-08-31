import { Link } from "react-router-dom";
import type { JobListItem } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { effortLabel, jobStatusLabel } from "../../lib/labels";
import { isStopped } from "../../lib/status";
import { StatusDot } from "../ui/StatusDot";
import { Tag, type TagTone } from "../ui/Tag";

function statusTag(job: JobListItem): { tone: TagTone; label: string } {
  const label = jobStatusLabel(job.status);
  if (job.status === "completed") return { tone: "ok", label };
  if (job.status === "failed") return { tone: "danger", label };
  if (job.status === "cancelled") return { tone: "warn", label };
  return { tone: "neutral", label };
}

function TrashIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.9 4.3h10.2M6.3 4.3V3.2a.9.9 0 0 1 .9-.9h1.6a.9.9 0 0 1 .9.9v1.1M4.4 4.3l.5 8.2a1 1 0 0 0 1 .9h4.2a1 1 0 0 0 1-.9l.5-8.2"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor" />
    </svg>
  );
}

export function JobItem({
  job,
  onDelete,
  onStop,
  busy,
}: {
  job: JobListItem;
  onDelete: () => void;
  onStop: () => void;
  busy: boolean;
}) {
  const done = job.status === "completed";
  const href = done ? `/jobs/${job.job_id}/report` : `/jobs/${job.job_id}`;
  const tag = statusTag(job);
  const stopped = isStopped(job.status);
  const name = job.question || "未命名研究";
  // 整行就是打开任务的链接，右侧那一个图标是行里唯一的另一个动作：停掉还在跑的，
  // 或者把已经停下来的移出历史。
  const action = stopped
    ? { label: `删除任务：${name}`, hint: "从任务历史里删除", icon: <TrashIcon />, act: onDelete }
    : { label: `终止任务：${name}`, hint: "终止这个研究任务", icon: <StopIcon />, act: onStop };
  // 已经在收尾的任务再点一次没有意义：取消请求早就记下了。
  const disabled = busy || job.status === "cancelling";
  return (
    <div className="job-item">
      <Link className="open" to={href}>
        <span className="st">
          <StatusDot status={job.status} outcome={job.outcome} verification={job.verification_status} />
        </span>
        <div className="main">
          <div className="q">{job.question || "（未命名研究）"}</div>
          <div className="sub">
            <Tag tone={tag.tone}>{tag.label}</Tag>
            {job.effort ? <span>{effortLabel(job.effort)}</span> : null}
            <span>{fmtDateTime(job.created_at)}</span>
          </div>
        </div>
      </Link>
      <button
        aria-label={action.label}
        className="act"
        disabled={disabled}
        onClick={action.act}
        title={job.status === "cancelling" ? "正在终止…" : action.hint}
        type="button"
      >
        {action.icon}
      </button>
    </div>
  );
}
