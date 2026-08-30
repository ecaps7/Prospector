export type StatusTone = "running" | "done" | "warn" | "danger" | "dim";

/**
 * The single place that decides what colour a job's status is. Three copies of
 * this mapping used to live in JobsPage, JobBar and MonitorPage, and they had
 * already drifted apart on `running` and `completed`.
 *
 * 完成的任务分不分色看交付判定：`partial` / `failed` 是报告没全站住，得和干净
 * 交付区分开。改版前的旧任务没有判定字段，成色写在 `outcome` 上，所以两个都读。
 */
export function statusTone(
  status: string,
  outcome?: string | null,
  verification?: string | null,
): StatusTone {
  if (status === "completed") {
    if (verification === "partial" || verification === "failed") return "warn";
    return outcome === "partial" ? "warn" : "done";
  }
  if (status === "failed") return "danger";
  if (status === "cancelled" || status === "cancelling") return "warn";
  if (status === "running") return "running";
  return "dim";
}
