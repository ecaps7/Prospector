import { NavLink, useMatch } from "react-router-dom";
import { StatusDot } from "./ui/StatusDot";
import { useSegThumb } from "./useSegThumb";

type Props = {
  jobId: string;
  question: string;
  status: string;
  outcome?: string | null;
};

export function JobBar({ jobId, question, status, outcome }: Props) {
  const onReport = Boolean(useMatch("/jobs/:jobId/report"));
  const { rootRef, thumbRef } = useSegThumb<HTMLDivElement>(onReport ? 1 : 0, 2);

  return (
    <div className="jobbar">
      <div className="jobbar-in">
        <NavLink className="back" to="/jobs" aria-label="返回任务列表">
          ‹ 任务列表
        </NavLink>
        <div className="jobid">
          <span className="st">
            <StatusDot status={status} outcome={outcome} />
          </span>
          <span className="q">{question}</span>
        </div>
        <div className="seg tabs" role="tablist" aria-label="任务视图切换" ref={rootRef}>
          <span className="seg-thumb" aria-hidden="true" ref={thumbRef} />
          <NavLink
            to={`/jobs/${jobId}`}
            end
            data-label="研究监控"
            className={({ isActive }) => (isActive ? "on" : "")}
            role="tab"
          >
            研究监控
          </NavLink>
          <NavLink
            to={`/jobs/${jobId}/report`}
            data-label="报告"
            className={({ isActive }) => (isActive ? "on" : "")}
            role="tab"
          >
            报告
          </NavLink>
        </div>
      </div>
    </div>
  );
}
