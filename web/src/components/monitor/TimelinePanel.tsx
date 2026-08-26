import { memo } from "react";
import type { TimelineEntry } from "../../state/timeline";

/** Memoised: `rows` only gets a new identity when an event actually lands. */
export const TimelinePanel = memo(function TimelinePanel({ rows }: { rows: TimelineEntry[] }) {
  return (
    <div className="card timeline-panel">
      <div className="panel-title">事件时间线</div>
      <div className="timeline">
        {rows.length === 0 ? <p className="muted">等待事件…</p> : null}
        {rows.map((row, index) => (
          <div className={`tl-row ${row.cls}`} key={`${row.createdAt}-${index}-${row.text}`}>
            <span className="tl-time">{row.createdAt}</span>
            <span className="tl-tag">{row.tag}</span>
            <span className="tl-text">{row.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
});
