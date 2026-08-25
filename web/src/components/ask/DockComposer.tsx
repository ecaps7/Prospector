import type { KeyboardEvent, RefObject } from "react";
import { AutoGrowTextarea } from "../ui/AutoGrowTextarea";

const MAX_HEIGHT = 160;

type Props = {
  draft: string;
  onDraftChange: (value: string) => void;
  /** The dock only accepts input while Scope is waiting on a clarification. */
  active: boolean;
  busy: boolean;
  canSend: boolean;
  placeholder: string;
  error: string | null;
  onSend: () => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
};

export function DockComposer({
  draft,
  onDraftChange,
  active,
  busy,
  canSend,
  placeholder,
  error,
  onSend,
  inputRef,
}: Props) {
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (active) onSend();
  };

  return (
    <div className="ask-dock">
      {error ? <p className="form-error">{error}</p> : null}
      <form
        className="card composer composer-dock"
        onSubmit={(event) => {
          event.preventDefault();
          if (active) onSend();
        }}
      >
        <label className="sr-only" htmlFor="ask-dock-composer">
          {active ? "澄清回答" : "研究问题"}
        </label>
        <AutoGrowTextarea
          id="ask-dock-composer"
          inputRef={inputRef}
          maxHeight={MAX_HEIGHT}
          rows={1}
          value={draft}
          disabled={!active || busy}
          onChange={(event) => onDraftChange(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
        />
        <button
          className="send-btn"
          type="submit"
          disabled={!active || !canSend}
          aria-label={active ? "发送回答" : "开始展开问题"}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path
              d="M8 12.5V3.5M8 3.5 3.8 7.6M8 3.5l4.2 4.1"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </form>
      {active ? <p className="composer-hint">Enter 发送 · Shift+Enter 换行</p> : null}
    </div>
  );
}
