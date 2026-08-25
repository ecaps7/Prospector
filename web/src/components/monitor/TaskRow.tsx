import type { ViewTask } from "../../state/jobView";
import { Meter } from "../ui/Meter";
import { Tag } from "../ui/Tag";

const STAGE_LABEL: Record<string, string> = {
  scout: "scout",
  deep_dive: "deep_dive",
  verify: "verify",
};

const MODE_LABEL: Record<string, string> = {
  factual: "事实核验",
  comparison: "对比",
  counterargument: "反证",
  risk_scan: "风险扫描",
  timeline: "时间线",
};

export function TaskRow({ task, index }: { task: ViewTask; index: number }) {
  const rowClass = task.status === "running" ? "running" : task.status === "pending" ? "queued" : "";
  return (
    <div className={`task-row ${rowClass}`}>
      <span className="task-idx">T{index + 1}</span>
      <div className="task-main">
        <div className="task-q">{task.question}</div>
        <div className="task-meta">
          <Tag tone="neutral">{STAGE_LABEL[task.researchStage] ?? task.researchStage}</Tag>
          <Tag tone="neutral">{MODE_LABEL[task.researchMode] ?? task.researchMode}</Tag>
        </div>
      </div>
      {task.status === "done" ? (
        <span className="task-status">
          <Tag tone="ok">✔ 收工</Tag>
        </span>
      ) : null}
      {task.status === "failed" ? (
        <span className="task-status">
          <Tag tone="danger">失败</Tag>
        </span>
      ) : null}
      {task.status === "running" ? (
        <span className="task-status">
          <span className="rounds-bar">
            <Meter pct={task.roundsLimit ? (task.roundsUsed / task.roundsLimit) * 100 : 0} />
            {task.roundsUsed}/{task.roundsLimit} 轮
          </span>
        </span>
      ) : null}
    </div>
  );
}
