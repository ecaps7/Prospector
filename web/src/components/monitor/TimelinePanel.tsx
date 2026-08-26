import { memo, useLayoutEffect, useRef, useState } from "react";
import type { TimelineEntry } from "../../state/timeline";
import { filterTimelineEntries, type TimelineFilter } from "../../state/timelineDisplay";
import { Segmented } from "../Segmented";

const FILTER_OPTIONS: { value: TimelineFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "key", label: "关键" },
];

/** 贴底判定的容差：滚动条停在离底不到这个距离时仍算“在底部”。 */
const AT_BOTTOM_SLACK = 24;

/** Memoised: `rows` only gets a new identity when an event actually lands. */
export const TimelinePanel = memo(function TimelinePanel({
  rows,
  historyLoading,
}: {
  rows: TimelineEntry[];
  historyLoading: boolean;
}) {
  const [filter, setFilter] = useState<TimelineFilter>("all");
  const [pinned, setPinned] = useState(true);
  const [unpinnedAtCount, setUnpinnedAtCount] = useState(rows.length);
  const scrollRef = useRef<HTMLDivElement>(null);

  const visible = filterTimelineEntries(rows, filter);
  const hidden = rows.length - visible.length;
  const unseen = pinned ? 0 : Math.max(0, rows.length - unpinnedAtCount);

  // 贴底跟随。只有用户本来就停在底部时才追新事件——手动上翻是在读历史，
  // 这时候把滚动位置抢走比不跟随更糟。pinned 进依赖是有意的：点“回到最新”
  // 只需要把它翻回 true，滚动交给这里做。
  //
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node || !pinned) return;
    node.scrollTop = node.scrollHeight;
  }, [rows, filter, pinned]);

  const handleScroll = () => {
    const node = scrollRef.current;
    if (!node) return;
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight <= AT_BOTTOM_SLACK;
    if (!atBottom && pinned) setUnpinnedAtCount(rows.length);
    setPinned(atBottom);
  };

  return (
    <div className="card timeline-panel">
      <div className="tl-head">
        <div className="panel-title">事件时间线</div>
        <Segmented options={FILTER_OPTIONS} value={filter} onChange={setFilter} label="时间线密度" />
      </div>
      <div className="timeline" ref={scrollRef} onScroll={handleScroll}>
        {visible.length === 0 ? (
          <p className="muted">
            {historyLoading ? "正在载入历史记录…" : rows.length ? `已折叠 ${hidden} 条工具调用` : "等待事件…"}
          </p>
        ) : null}
        {visible.map((entry, index) => (
          <div
            className={`tl-row${entry.cls ? ` tl-${entry.cls}` : ""}`}
            key={`${entry.eventId}-${index}`}
          >
            <span className="tl-time">{entry.createdAt}</span>
            <span className="tl-tag">{entry.tag}</span>
            <span className="tl-text">{entry.text}</span>
          </div>
        ))}
      </div>
      {pinned ? null : (
        <button
          type="button"
          className="tl-jump"
          onClick={() => {
            setPinned(true);
          }}
        >
          {unseen ? `新增 ${unseen} 条 · ` : ""}回到最新 ↓
        </button>
      )}
    </div>
  );
});
