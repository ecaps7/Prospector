import { useEffect, useRef, useState } from "react";
import { emptyConstraints } from "../../api/client";
import { effortLabel, languageLabel } from "../../lib/labels";
import type { ResearchBrief, UserConstraints } from "../../api/types";
import { AutoGrowTextarea } from "../ui/AutoGrowTextarea";
import { Spinner } from "../ui/Status";
import { Chip, Tag } from "../ui/Tag";

/** Mac shows ⌘, everything else Ctrl. Only ever rendered inside a hint. */
const RUN_HINT = /Mac|iPhone|iPad/.test(navigator.userAgent) ? "⌘↩" : "Ctrl↩";

function constraintsEmpty(value: UserConstraints): boolean {
  return !(
    value.time_range ||
    value.regions.length ||
    value.comparison_targets.length ||
    value.source_rules.length ||
    value.exclusions.length ||
    value.deliverable_rules.length
  );
}

function constraintRows(value: UserConstraints): { key: string; label: string; text: string }[] {
  const rows: { key: string; label: string; text: string }[] = [];
  if (value.time_range) rows.push({ key: "time", label: "时间", text: value.time_range });
  if (value.regions.length) rows.push({ key: "regions", label: "地区", text: value.regions.join("、") });
  if (value.comparison_targets.length) {
    rows.push({ key: "compare", label: "对比", text: value.comparison_targets.join("、") });
  }
  if (value.source_rules.length) rows.push({ key: "source", label: "来源", text: value.source_rules.join("、") });
  if (value.exclusions.length) rows.push({ key: "exclude", label: "排除", text: value.exclusions.join("、") });
  if (value.deliverable_rules.length) {
    rows.push({ key: "deliver", label: "交付", text: value.deliverable_rules.join("、") });
  }
  return rows;
}

type Props = {
  brief: ResearchBrief;
  tag: "pending" | "revised";
  /** Scope's own wording, kept so "已修改 / 还原" has something to compare against. */
  briefTextOriginal: string;
  reviseOpen: boolean;
  reviseNote: string;
  /** A revision is in flight — same slow Scope service, so it shows and can be stopped. */
  revising: boolean;
  onStopRevise: () => void;
  onBriefTextChange: (text: string) => void;
  onRestoreBriefText: () => void;
  onReviseNoteChange: (note: string) => void;
  onRevise: () => void;
  onToggleRevise: () => void;
  onConfirm: () => void;
  onQuit: () => void;
};

export function BriefCard({
  brief,
  tag,
  briefTextOriginal,
  reviseOpen,
  reviseNote,
  revising,
  onStopRevise,
  onBriefTextChange,
  onRestoreBriefText,
  onReviseNoteChange,
  onRevise,
  onToggleRevise,
  onConfirm,
  onQuit,
}: Props) {
  const constraints = brief.user_constraints ?? emptyConstraints();
  const cardRef = useRef<HTMLDivElement>(null);
  const reviseRef = useRef<HTMLInputElement>(null);
  const [modHeld, setModHeld] = useState(false);
  const dirty = brief.brief_text !== briefTextOriginal;

  // Without this the Brief arrives with focus stranded on <body>, which leaves nothing
  // for a screen reader to announce and no sensible place for Tab to start from.
  useEffect(() => {
    cardRef.current?.focus({ preventScroll: true });
  }, []);

  // Opening the revision row and then having to click into it would waste the click.
  useEffect(() => {
    if (reviseOpen) reviseRef.current?.focus();
  }, [reviseOpen]);

  // The shortcut hints stay hidden until a modifier is down: keyboard users find them
  // when they reach for the keyboard, and everyone else gets an uncluttered button row.
  useEffect(() => {
    const sync = (event: KeyboardEvent) => setModHeld(event.metaKey || event.ctrlKey);
    const clear = () => setModHeld(false);
    window.addEventListener("keydown", sync);
    window.addEventListener("keyup", sync);
    window.addEventListener("blur", clear);
    return () => {
      window.removeEventListener("keydown", sync);
      window.removeEventListener("keyup", sync);
      window.removeEventListener("blur", clear);
    };
  }, []);

  return (
    <div
      className={`card brief-card${modHeld ? " mod-held" : ""}`}
      ref={cardRef}
      tabIndex={-1}
      role="group"
      aria-label="Research Brief"
    >
      <div className="brief-head">
        <div className="brief-meta">
          <span className="brief-kind">研究方案</span>
          <Tag tone={tag === "pending" ? "warn" : "ok"}>{tag === "pending" ? "待确认" : "修订稿"}</Tag>
          {dirty ? (
            <span className="brief-dirty">
              已修改
              <button className="brief-undo" type="button" onClick={onRestoreBriefText}>
                还原
              </button>
            </span>
          ) : null}
          <Chip mono>
            {effortLabel(brief.effort)} · {languageLabel(brief.language)}
          </Chip>
        </div>
        <h3>{brief.question}</h3>
      </div>
      <div className="brief-body">
        <div className="brief-field">
          <div className="k">
            研究范围
            <span className="hint">根据你的问题展开，研究过程中会按需取舍</span>
          </div>
          <AutoGrowTextarea
            className="brief-text"
            aria-label="研究范围"
            title="点进来可以直接改这段文字"
            spellCheck={false}
            value={brief.brief_text}
            onChange={(event) => onBriefTextChange(event.target.value)}
          />
        </div>
        <div className="brief-field">
          <div className="k">
            你的限定条件
            <span className="hint">取自你的原话，研究全程不会突破</span>
          </div>
          {constraintsEmpty(constraints) ? (
            <div className="constraint-empty">
              你没有限定时间、地区、对比对象或资料来源，研究范围不受额外约束。
            </div>
          ) : (
            <div className="constraint-list">
              {constraintRows(constraints).map((row) => (
                <div className="row" key={row.key}>
                  <span className="k">{row.label}</span>
                  <span>{row.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="brief-foot">
        <button
          className={`btn ghost${reviseOpen ? " on" : ""}`}
          type="button"
          onClick={onToggleRevise}
          aria-expanded={reviseOpen}
          aria-controls="brief-revise"
        >
          编辑
        </button>
        <div className="spacer" />
        <button className="btn ghost" type="button" onClick={onQuit}>
          取消
          <kbd className="hot">Esc</kbd>
        </button>
        <button className="btn primary" type="button" onClick={onConfirm} disabled={revising}>
          开始
          <kbd className="hot">{RUN_HINT}</kbd>
        </button>
      </div>
      <div className={`revise-row${reviseOpen ? " open" : ""}`} id="brief-revise">
        <input
          ref={reviseRef}
          value={reviseNote}
          onChange={(event) => onReviseNoteChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
            event.preventDefault();
            onRevise();
          }}
          placeholder="说说要怎么改，例如：多看早期的社区讨论"
          disabled={revising}
        />
        {revising ? (
          <button className="btn ghost sm" type="button" onClick={onStopRevise}>
            <Spinner inline /> 停止
          </button>
        ) : (
          <button className="btn ghost sm" type="button" onClick={onRevise}>
            重写
          </button>
        )}
      </div>
    </div>
  );
}
