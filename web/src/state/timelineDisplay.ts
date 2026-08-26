export type TimelineDisplayClass =
  | "planner"
  | "tool"
  | "round"
  | "evidence"
  | "gap"
  | "phase"
  | "done"
  | "";

export type TimelineDisplayEntry = {
  eventId: number;
  createdAt: string;
  tag: string;
  text: string;
  cls: TimelineDisplayClass;
};

export type TimelineFilter = "all" | "key";

export function timelineClock(value: string | null | undefined): string {
  if (!value) return "--:--:--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "--:--:--";
  return parsed.toLocaleTimeString("en-GB", { hour12: false });
}

export function appendTimelineEntries<T>(current: T[], entries: T[]): T[] {
  return [...current, ...entries];
}

export function filterTimelineEntries<T extends { cls: TimelineDisplayClass }>(
  rows: T[],
  filter: TimelineFilter,
): T[] {
  if (filter === "all") return rows;
  return rows.filter((row) => row.cls !== "tool" && row.cls !== "round");
}
