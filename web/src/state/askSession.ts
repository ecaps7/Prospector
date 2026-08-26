import type { EffortLevel, ResearchBrief } from "../api/types";

export type ClarifyTurn = {
  question: string;
  answer: string;
};

export type AskState =
  | { step: "compose" }
  | { step: "scoping"; question: string; turns: ClarifyTurn[] }
  | {
      step: "clarify";
      question: string;
      clarificationQuestion: string;
      turns: ClarifyTurn[];
    }
  | {
      step: "brief";
      question: string;
      turns: ClarifyTurn[];
      brief: ResearchBrief;
      tag: "pending" | "revised";
      /**
       * Scope 交付时的原始范围文本。正文是常驻可编辑的，没有"编辑模式"可以标记，
       * 所以是否改过只能靠和这份原稿比对——它也是"还原"按回去的那一份。
       */
      briefTextOriginal: string;
      reviseOpen: boolean;
      reviseNote: string;
    };

export type AskSession = {
  draft: string;
  effort: EffortLevel;
  language: string;
  state: AskState;
};

const KEY = "prospector-ask-v1";

const EMPTY: AskSession = { draft: "", effort: "standard", language: "zh", state: { step: "compose" } };

function isBrief(value: unknown): value is ResearchBrief {
  if (!value || typeof value !== "object") return false;
  const brief = value as Partial<ResearchBrief>;
  return typeof brief.question === "string" && typeof brief.brief_text === "string";
}

/**
 * A reloaded page has no in-flight Scope call, so a persisted `scoping` step can
 * never resolve. Degrade it the same way a failed Scope does: back to the
 * composer with the question returned to the draft.
 */
function reviveState(value: unknown, draft: string): { state: AskState; draft: string } {
  if (!value || typeof value !== "object") return { state: { step: "compose" }, draft };
  const raw = value as { step?: unknown; question?: unknown; turns?: unknown };
  const turns = Array.isArray(raw.turns) ? (raw.turns as ClarifyTurn[]) : [];
  const question = typeof raw.question === "string" ? raw.question : "";

  if (raw.step === "scoping" && question) return { state: { step: "compose" }, draft: draft || question };

  if (raw.step === "clarify") {
    const { clarificationQuestion } = value as { clarificationQuestion?: unknown };
    if (question && typeof clarificationQuestion === "string") {
      return { state: { step: "clarify", question, clarificationQuestion, turns }, draft };
    }
  }

  if (raw.step === "brief") {
    const { brief, tag, briefTextOriginal } = value as {
      brief?: unknown;
      tag?: unknown;
      briefTextOriginal?: unknown;
    };
    if (question && isBrief(brief)) {
      return {
        state: {
          step: "brief",
          question,
          turns,
          brief,
          tag: tag === "revised" ? "revised" : "pending",
          // 存过的原稿优先；旧版本的存档没有这个字段，就把眼下这份当原稿——
          // 顶多是"已修改"标记丢一次，总好过误报成改过。
          briefTextOriginal: typeof briefTextOriginal === "string" ? briefTextOriginal : brief.brief_text,
          reviseOpen: false,
          reviseNote: "",
        },
        draft,
      };
    }
  }

  return { state: { step: "compose" }, draft };
}

/** Reads the in-progress Ask flow back after a reload. Never throws. */
export function loadAskSession(): AskSession {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<AskSession>;
    const draft = typeof parsed.draft === "string" ? parsed.draft : "";
    const revived = reviveState(parsed.state, draft);
    return {
      draft: revived.draft,
      effort: parsed.effort === "quick" || parsed.effort === "deep" ? parsed.effort : "standard",
      language: typeof parsed.language === "string" ? parsed.language : "zh",
      state: revived.state,
    };
  } catch {
    return EMPTY;
  }
}

export function saveAskSession(session: AskSession): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* private mode / quota — persistence is a convenience, not a requirement */
  }
}

export function clearAskSession(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* see saveAskSession */
  }
}

/** True when the session carries work worth telling the user was restored. */
export function isRestorable(state: AskState): boolean {
  return state.step === "brief" || state.step === "clarify";
}
