import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import type { JobDetail } from "../../api/types";
import { phaseIndex } from "../../state/jobView";
import { gateStageName, isGateLive, reportGateCopy, type ReportGateKind } from "../../state/reportGate";
import { PhaseTrack } from "../monitor/PhaseTrack";
import { Spinner } from "../ui/Status";
import { StatusDot } from "../ui/StatusDot";

type Props = {
  job: JobDetail;
  kind: Exclude<ReportGateKind, "ready">;
};

function elapsedSeconds(job: JobDetail, now: number, live: boolean): number {
  const start = new Date(job.created_at).getTime();
  const end = live ? now : new Date(job.updated_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return 0;
  return Math.max(0, Math.floor((end - start) / 1000));
}

/** 撰写阶段才摆版式：报告确实快出来了，占位块是预告；更早的阶段摆它就是骗人。 */
function DraftSkeleton() {
  return (
    <div className="rp-skeleton" aria-hidden="true">
      <span className="sk sk-h" />
      <span className="sk" />
      <span className="sk" />
      <span className="sk sk-short" />
      <span className="sk sk-h" />
      <span className="sk" />
      <span className="sk sk-short" />
    </div>
  );
}

/**
 * 报告还没有时这一页显示什么。它不提供"重试"：任务快照在 App 里持续轮询，
 * 报告一落库这一页自己就换成正文；而已经取消或失败的任务，重试也变不出报告来。
 */
export function ReportPending({ job, kind }: Props) {
  const live = isGateLive(kind);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);

  const copy = reportGateCopy(job, kind, elapsedSeconds(job, now, live));

  return (
    <section className="view">
      <div className="report-pending">
        <div className="rp-head">
          {live ? <Spinner /> : <StatusDot status={job.status} outcome={job.outcome} />}
          <h1>{copy.title}</h1>
        </div>
        {copy.detail ? <p className="rp-detail">{copy.detail}</p> : null}
        {gateStageName(job) ? (
          <div className="card rp-track">
            <PhaseTrack phaseIndex={phaseIndex(job.phase)} status={job.status} />
          </div>
        ) : null}
        <div className="rp-foot">
          {live ? <span className="muted">报告生成后这一页会自动显示，不用刷新。</span> : null}
          <NavLink className="btn ghost sm" to={`/jobs/${job.job_id}`}>
            查看研究监控
          </NavLink>
        </div>
        {kind === "composing" ? <DraftSkeleton /> : null}
      </div>
    </section>
  );
}
