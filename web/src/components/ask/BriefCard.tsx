import { emptyConstraints } from "../../api/client";
import type { ResearchBrief, UserConstraints } from "../../api/types";
import { Chip, Tag } from "../ui/Tag";

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
  editing: boolean;
  reviseOpen: boolean;
  reviseNote: string;
  onBriefTextChange: (text: string) => void;
  onReviseNoteChange: (note: string) => void;
  onRevise: () => void;
  onToggleRevise: () => void;
  onToggleEdit: () => void;
  onConfirm: () => void;
  onQuit: () => void;
};

export function BriefCard({
  brief,
  tag,
  editing,
  reviseOpen,
  reviseNote,
  onBriefTextChange,
  onReviseNoteChange,
  onRevise,
  onToggleRevise,
  onToggleEdit,
  onConfirm,
  onQuit,
}: Props) {
  const constraints = brief.user_constraints ?? emptyConstraints();
  return (
    <div className="card brief-card">
      <div className="brief-head">
        <h3>Research Brief</h3>
        <Tag tone={tag === "pending" ? "warn" : "ok"}>{tag === "pending" ? "待确认" : "修订稿"}</Tag>
        <Chip mono>
          {brief.effort} · {brief.language}
        </Chip>
      </div>
      <div className="brief-body">
        <div className="brief-field">
          <div className="k">研究问题</div>
          <div className="v">{brief.question}</div>
        </div>
        <div className="brief-field">
          <div className="k">研究空间 · BRIEF_TEXT（Scope 展开，Planner 可取舍）</div>
          {editing ? (
            <textarea
              className="brief-text editing"
              value={brief.brief_text}
              onChange={(event) => onBriefTextChange(event.target.value)}
            />
          ) : (
            <div className="brief-text">{brief.brief_text}</div>
          )}
        </div>
        <div className="brief-field">
          <div className="k">用户约束 · USER_CONSTRAINTS（用户亲口所述，不可违背）</div>
          {constraintsEmpty(constraints) ? (
            <div className="constraint-empty">
              时间范围 / 地区 / 对比对象 / 来源规则 / 排除项均为空 —— 用户未施加任何范围约束
            </div>
          ) : (
            <div className="constraint-list">
              {constraintRows(brief.user_constraints).map((row) => (
                <div className="row" key={row.key}>
                  <span className="k">{row.label}</span>
                  <span>{row.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className={`revise-row${reviseOpen ? " open" : ""}`}>
        <input
          value={reviseNote}
          onChange={(event) => onReviseNoteChange(event.target.value)}
          placeholder="输入一条修订指令，例如：增加对早期讨论的考察"
        />
        <button className="btn ghost sm" type="button" onClick={onRevise}>
          修订
        </button>
      </div>
      <div className="brief-foot">
        <button className="btn primary" type="button" onClick={onConfirm}>
          确认，开始研究
        </button>
        <kbd>C</kbd>
        <div className="spacer" />
        <div className="quiet-acts">
          <button type="button" onClick={onToggleRevise}>
            指令修订 <kbd>I</kbd>
          </button>
          <button type="button" onClick={onToggleEdit}>
            {editing ? "保存编辑" : <>编辑 <kbd>E</kbd></>}
          </button>
          <button type="button" onClick={onQuit}>
            放弃 <kbd>Q</kbd>
          </button>
        </div>
      </div>
    </div>
  );
}
