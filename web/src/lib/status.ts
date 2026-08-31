export type StatusTone = "running" | "done" | "warn" | "danger" | "dim";

/**
 * The single place that decides what colour a job's status is. Three copies of
 * this mapping used to live in JobsPage, JobBar and MonitorPage, and they had
 * already drifted apart on `running` and `completed`.
 *
 * 任务状态只反映生命周期，不显示报告核验判定。
 */
export function statusTone(
  status: string,
  _outcome?: string | null,
  _verification?: string | null,
): StatusTone {
  if (status === "completed") return "done";
  if (status === "failed") return "danger";
  if (status === "cancelled" || status === "cancelling") return "warn";
  if (status === "queued" || status === "running") return "running";
  return "dim";
}

/**
 * 任务是否已经停下来。停下来的任务才可以从历史里删除；`cancelling` 还在收尾，
 * 调度器手上还攥着它。判定同样只放这一处，别在页面里重新拼状态集合。
 */
export function isStopped(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}
