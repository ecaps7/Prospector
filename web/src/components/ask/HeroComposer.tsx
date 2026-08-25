import type { RefObject } from "react";
import type { EffortLevel } from "../../api/types";
import { Segmented } from "../Segmented";
import { AutoGrowTextarea } from "../ui/AutoGrowTextarea";

const MAX_HEIGHT = 220;

const EFFORT_OPTIONS: { value: EffortLevel; label: string }[] = [
  { value: "quick", label: "快速" },
  { value: "standard", label: "标准" },
  { value: "deep", label: "深入" },
];

const LANGUAGE_OPTIONS = [
  { value: "zh", label: "中文" },
  { value: "en", label: "English" },
];

type Props = {
  draft: string;
  onDraftChange: (value: string) => void;
  effort: EffortLevel;
  onEffortChange: (value: EffortLevel) => void;
  language: string;
  onLanguageChange: (value: string) => void;
  canSend: boolean;
  onSubmit: () => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
};

export function HeroComposer({
  draft,
  onDraftChange,
  effort,
  onEffortChange,
  language,
  onLanguageChange,
  canSend,
  onSubmit,
  inputRef,
}: Props) {
  return (
    <div className="card composer">
      <label className="sr-only" htmlFor="ask-composer">
        研究问题
      </label>
      <AutoGrowTextarea
        id="ask-composer"
        inputRef={inputRef}
        maxHeight={MAX_HEIGHT}
        resetWhenEmpty
        value={draft}
        onChange={(event) => onDraftChange(event.target.value)}
        placeholder="提一个需要深度研究的问题"
      />
      <div className="composer-bar">
        <div className="composer-opts">
          <span className="field-label">研究档位</span>
          <Segmented label="研究档位" value={effort} onChange={onEffortChange} options={EFFORT_OPTIONS} />
          <span className="field-label">语言</span>
          <Segmented label="报告语言" value={language} onChange={onLanguageChange} options={LANGUAGE_OPTIONS} />
        </div>
        <button className="btn primary" type="button" onClick={onSubmit} disabled={!canSend}>
          开始展开问题
        </button>
      </div>
    </div>
  );
}
