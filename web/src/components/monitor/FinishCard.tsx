import { Link } from "react-router-dom";
import { fmtClock, fmtNum } from "../../lib/format";
import { errorLabel, jobStatusLabel, outcomeLabel, phaseLabel } from "../../lib/labels";

type Props = {
  status: string;
  phase: string;
  outcome: string | null;
  errorCode: string | null;
  elapsed: number;
  tokens: number;
  tools: number;
  onOpenReport: () => void;
};

export function FinishCard({
  status,
  phase,
  outcome,
  errorCode,
  elapsed,
  tokens,
  tools,
  onOpenReport,
}: Props) {
  const completed = status === "completed";
  const failed = status === "failed";
  return (
    <div className="card finish-card">
      <h3>
        <span className={completed ? "ok-dot" : failed ? "fail-dot" : "warn-dot"}>
          {completed ? "✓" : failed ? "!" : "⊘"}
        </span>
        {completed
          ? `研究完成 · ${fmtClock(elapsed)}`
          : failed
            ? `研究失败${errorCode ? ` · ${errorLabel(errorCode)}` : ""}`
            : "任务已取消"}
      </h3>
      <div className="finish-rows">
        <div className="item">
          <div className="k">阶段</div>
          <div className="v">{phaseLabel(phase)}</div>
        </div>
        <div className="item">
          <div className="k">结果</div>
          <div className="v">{outcomeLabel(outcome) || jobStatusLabel(status)}</div>
        </div>
        <div className="item">
          <div className="k">用量</div>
          <div className="v">
            {fmtNum(tokens)} Token · {tools} 次工具调用
          </div>
        </div>
      </div>
      <div className="acts">
        {completed ? (
          <button className="btn primary" type="button" onClick={onOpenReport}>
            查看报告
          </button>
        ) : null}
        <Link className="btn ghost" to="/jobs">
          任务历史
        </Link>
      </div>
    </div>
  );
}
