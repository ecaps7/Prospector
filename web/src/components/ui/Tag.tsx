import type { ReactNode } from "react";

export type TagTone = "ok" | "warn" | "danger" | "neutral" | "replan";

export function Tag({ tone, children }: { tone: TagTone; children: ReactNode }) {
  return <span className={`tag ${tone}`}>{children}</span>;
}

export function Chip({ mono, children }: { mono?: boolean; children: ReactNode }) {
  return <span className={`chip soft${mono ? " mono" : ""}`}>{children}</span>;
}
