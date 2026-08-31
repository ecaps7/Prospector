import { memo, useState } from "react";
import type { PlanPage } from "../../state/jobView";
import { Chip, Tag } from "../ui/Tag";
import { TaskRow } from "./TaskRow";

type Props = {
  pages: PlanPage[];
  roundsLeft: number;
};

/** Memoised for the same reason as TimelinePanel: the per-second clock must not
 *  drag the whole task table through a re-render. */
export const PlanPanel = memo(function PlanPanel({ pages, roundsLeft }: Props) {
  // null = 跟随最新一轮。翻回最后一页就自动恢复跟随，不用另设开关。
  const [wanted, setWanted] = useState<number | null>(null);
  const total = pages.length;
  const index = wanted === null ? total - 1 : Math.min(wanted, total - 1);
  const page = pages[index];
  // planPages 至少给一页，这只是别让空数组把整个监控页打崩。
  if (!page) return null;

  const goto = (next: number) => {
    const clamped = Math.max(0, Math.min(next, total - 1));
    setWanted(clamped === total - 1 ? null : clamped);
  };

  return (
    <div className="card plan-panel">
      <div className="plan-head">
        <span className="ver">研究计划 第 {page.planVersion} 版</span>
        {page.planVersion > 1 ? <Tag tone="replan">已调整</Tag> : null}
        <Chip mono>余 {Math.max(0, roundsLeft)} 轮</Chip>
        {total > 1 || page.round > 0 ? (
          <div className="plan-nav">
            <button
              type="button"
              onClick={() => goto(index - 1)}
              disabled={index === 0}
              aria-label="上一轮派发"
            >
              ‹
            </button>
            {/* 「轮 N 派发」而不是「第 N 轮」：不是每一轮都会派发——判定收尾的那些轮
                不产生计划，所以翻页时轮号本来就会跳号（第 2 轮被核验交回后，第 3 轮才是
                第 2 版计划）。写法与时间线的 `[轮 N] 派发 …` 一致，两处可以直接对照。 */}
            <span className="plan-nav-at mono">
              轮 {page.round || 1} 派发 · {index + 1}/{total}
            </span>
            <button
              type="button"
              onClick={() => goto(index + 1)}
              disabled={index === total - 1}
              aria-label="下一轮派发"
            >
              ›
            </button>
          </div>
        ) : null}
      </div>
      <p className="plan-note">{page.reason || "等待第一次任务规划…"}</p>
      <div>
        {page.tasks.length === 0 ? <p className="muted">尚未派发任务</p> : null}
        {page.tasks.map(({ task, index: taskIndex }) => (
          <TaskRow task={task} index={taskIndex} key={task.taskId} />
        ))}
      </div>
    </div>
  );
});
