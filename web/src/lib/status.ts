export type StatusTone = "running" | "done" | "warn" | "danger" | "dim";

/**
 * The single place that decides what colour a job's status is. Three copies of
 * this mapping used to live in JobsPage, JobBar and MonitorPage, and they had
 * already drifted apart on `running` and `completed`.
 */
export function statusTone(status: string, outcome?: string | null): StatusTone {
  if (status === "completed") return outcome === "partial" ? "warn" : "done";
  if (status === "failed") return "danger";
  if (status === "cancelled" || status === "cancelling") return "warn";
  if (status === "running") return "running";
  return "dim";
}
