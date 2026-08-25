import { NavLink } from "react-router-dom";

type Props = {
  jobId: string;
  question: string;
  status: string;
};

function statusDot(status: string): string {
  if (status === "completed") return "ok";
  if (status === "failed") return "danger";
  if (status === "cancelled" || status === "cancelling") return "warn";
  return "dim";
}

export function JobBar({ jobId, question, status }: Props) {
  return (
    <div className="jobbar">
      <div className="jobbar-in">
        <NavLink className="back" to="/jobs" aria-label="返回任务列表">
          ‹ 任务列表
        </NavLink>
        <div className="jobid">
          <span className="st">
            <span className={`dot ${statusDot(status)}`} />
          </span>
          <span className="q">{question}</span>
        </div>
        <div className="tabs" role="tablist" aria-label="任务视图切换">
          <NavLink to={`/jobs/${jobId}`} end className={({ isActive }) => (isActive ? "on" : "")} role="tab">
            研究监控
          </NavLink>
          <NavLink to={`/jobs/${jobId}/report`} className={({ isActive }) => (isActive ? "on" : "")} role="tab">
            报告
          </NavLink>
        </div>
      </div>
    </div>
  );
}
