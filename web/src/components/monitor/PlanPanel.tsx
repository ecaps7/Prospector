import { memo } from "react";
import type { ViewTask } from "../../state/jobView";
import { Chip, Tag } from "../ui/Tag";
import { TaskRow } from "./TaskRow";

type Props = {
  planVersion: number;
  planReason: string | null;
  roundsLeft: number;
  tasks: ViewTask[];
};

/** Memoised for the same reason as TimelinePanel: the per-second clock must not
 *  drag the whole task table through a re-render. */
export const PlanPanel = memo(function PlanPanel({ planVersion, planReason, roundsLeft, tasks }: Props) {
  return (
    <div className="card plan-panel">
      <div className="plan-head">
        <span className="ver">研究计划 第 {planVersion} 版</span>
        {planVersion > 1 ? <Tag tone="replan">已调整</Tag> : null}
        <Chip mono>余 {Math.max(0, roundsLeft)} 轮</Chip>
      </div>
      <p className="plan-note">{planReason || "等待第一次任务规划…"}</p>
      <div>
        {tasks.length === 0 ? <p className="muted">尚未派发任务</p> : null}
        {tasks.map((task, index) => (
          <TaskRow task={task} index={index} key={task.taskId} />
        ))}
      </div>
    </div>
  );
});
