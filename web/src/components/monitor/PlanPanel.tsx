import type { ViewTask } from "../../state/jobView";
import { Chip, Tag } from "../ui/Tag";
import { TaskRow } from "./TaskRow";

type Props = {
  planVersion: number;
  planReason: string | null;
  roundsLeft: number;
  tasks: ViewTask[];
};

export function PlanPanel({ planVersion, planReason, roundsLeft, tasks }: Props) {
  return (
    <div className="card plan-panel">
      <div className="plan-head">
        <span className="ver">Plan v{planVersion}</span>
        {planVersion > 1 ? <Tag tone="replan">Replan</Tag> : null}
        <Chip mono>余 {Math.max(0, roundsLeft)} 轮</Chip>
      </div>
      <p className="plan-note">{planReason || "等待 Planner 首个决策…"}</p>
      <div>
        {tasks.length === 0 ? <p className="muted">尚未派发任务</p> : null}
        {tasks.map((task, index) => (
          <TaskRow task={task} index={index} key={task.taskId} />
        ))}
      </div>
    </div>
  );
}
