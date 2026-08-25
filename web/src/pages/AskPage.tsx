import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { EffortLevel, ResearchBrief } from "../api/types";
import { BriefCard } from "../components/ask/BriefCard";
import { ChatMessage } from "../components/ask/ChatMessage";
import { DockComposer } from "../components/ask/DockComposer";
import { ExampleQuestions } from "../components/ask/ExampleQuestions";
import { HeroComposer } from "../components/ask/HeroComposer";
import { useToast } from "../components/Toast";
import { StatusLine } from "../components/ui/Status";

type ClarifyTurn = {
  question: string;
  answer: string;
};

type AskState =
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
      editing: boolean;
      reviseOpen: boolean;
      reviseNote: string;
    };

export function AskPage() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [draft, setDraft] = useState("");
  const [effort, setEffort] = useState<EffortLevel>("standard");
  const [language, setLanguage] = useState("zh");
  const [state, setState] = useState<AskState>({ step: "compose" });
  const [error, setError] = useState<string | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const idle = state.step === "compose";
  const busy = state.step === "scoping";
  const canSend = (idle || state.step === "clarify") && !busy && Boolean(draft.trim());

  const runScope = useCallback(async (nextQuestion: string): Promise<void> => {
    setError(null);
    setState({ step: "scoping", question: nextQuestion, turns: [] });
    try {
      const outcome = await api.scope({
        question: nextQuestion,
        effort,
        language,
      });
      if (outcome.kind === "clarify") {
        setState({
          step: "clarify",
          question: nextQuestion,
          clarificationQuestion: outcome.clarification_question,
          turns: [],
        });
      } else {
        setState({
          step: "brief",
          question: nextQuestion,
          turns: [],
          brief: outcome.brief,
          tag: "pending",
          editing: false,
          reviseOpen: false,
          reviseNote: "",
        });
      }
    } catch (err) {
      setState({ step: "compose" });
      setDraft(nextQuestion);
      setError(err instanceof ApiError ? err.message : "Scope 服务不可用");
    }
  }, [effort, language]);

  const submitQuestion = () => {
    const trimmed = draft.trim();
    if (!trimmed) {
      toast("请先输入一个研究问题");
      return;
    }
    setDraft("");
    void runScope(trimmed);
  };

  const sendClarify = () => {
    if (state.step !== "clarify") return;
    const answer = draft.trim();
    if (!answer) {
      toast("请输入澄清回答");
      return;
    }
    const nextTurns = [...state.turns, { question: state.clarificationQuestion, answer }];
    const clarificationQuestion = state.clarificationQuestion;
    const question = state.question;
    setDraft("");
    void (async () => {
      setError(null);
      setState({ step: "scoping", question, turns: nextTurns });
      try {
        const outcome = await api.scope({
          question,
          effort,
          language,
          clarification_question: clarificationQuestion,
          clarification_answer: answer,
        });
        if (outcome.kind === "clarify") {
          setState({
            step: "clarify",
            question,
            clarificationQuestion: outcome.clarification_question,
            turns: nextTurns,
          });
        } else {
          setState({
            step: "brief",
            question,
            turns: nextTurns,
            brief: outcome.brief,
            tag: "pending",
            editing: false,
            reviseOpen: false,
            reviseNote: "",
          });
        }
      } catch (err) {
        setState({
          step: "clarify",
          question,
          clarificationQuestion,
          turns: nextTurns,
        });
        setDraft(answer);
        setError(err instanceof ApiError ? err.message : "Scope 服务不可用");
      }
    })();
  };

  const confirmBrief = useCallback(async () => {
    if (state.step !== "brief") return;
    try {
      const created = await api.createJob(state.brief);
      toast(`任务已创建：${created.job_id.slice(0, 8)}`);
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "无法创建任务");
    }
  }, [navigate, state, toast]);

  const reviseBrief = async () => {
    if (state.step !== "brief") return;
    const note = state.reviseNote.trim();
    if (!note) {
      toast("请输入一条修订指令");
      return;
    }
    setError(null);
    try {
      const { brief } = await api.reviseScope({
        question: state.question,
        previous_brief: state.brief,
        revision_note: note,
        effort: state.brief.effort,
        language: state.brief.language,
      });
      setState({ ...state, brief, tag: "revised", reviseOpen: false, reviseNote: "" });
      toast("Brief 已按指令重写一轮");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "修订失败");
    }
  };

  const quitBrief = useCallback(() => {
    if (state.step === "brief" || state.step === "scoping" || state.step === "clarify") {
      setDraft(state.question);
    }
    setState({ step: "compose" });
    toast("已放弃，未创建任务");
  }, [state, toast]);

  useEffect(() => {
    if (state.step !== "brief") return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.isContentEditable)) {
        return;
      }
      const key = event.key.toLowerCase();
      if (key === "c") void confirmBrief();
      if (key === "e") {
        setState((current) => (current.step === "brief" ? { ...current, editing: !current.editing } : current));
      }
      if (key === "i") {
        setState((current) => (current.step === "brief" ? { ...current, reviseOpen: !current.reviseOpen } : current));
      }
      if (key === "q") quitBrief();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmBrief, quitBrief, state.step]);

  useEffect(() => {
    if (state.step === "compose" || state.step === "clarify") {
      textareaRef.current?.focus();
    }
  }, [state.step]);

  useEffect(() => {
    if (idle) return;
    const node = stageRef.current;
    if (!node) return;
    node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  }, [idle, state, error]);

  const dockPlaceholder =
    state.step === "clarify"
      ? "回答上方的澄清问题…"
      : state.step === "brief"
        ? "确认上方 Brief 后开始研究"
        : "正在展开问题…";

  return (
    <section className={`view ask-view${idle ? " is-idle" : ""}`}>
      <div className="ask-stage" ref={stageRef}>
        <div className="ask-stage-inner">
          {state.step === "compose" ? (
            <>
              <div className="ask-hero">
                <h1>Prospector 深度研究智能体</h1>
              </div>
              <HeroComposer
                draft={draft}
                onDraftChange={setDraft}
                effort={effort}
                onEffortChange={setEffort}
                language={language}
                onLanguageChange={setLanguage}
                canSend={canSend}
                onSubmit={submitQuestion}
                inputRef={textareaRef}
              />
              {error ? <p className="form-error">{error}</p> : null}
              <ExampleQuestions
                onPick={(example) => {
                  setDraft(example);
                  textareaRef.current?.focus();
                }}
              />
            </>
          ) : (
            <div className="flow">
              <ChatMessage from="user">{state.question}</ChatMessage>

              {state.turns.map((turn, index) => (
                <div key={`${turn.question}-${index}`}>
                  <ChatMessage from="scope">{turn.question}</ChatMessage>
                  <ChatMessage from="user">{turn.answer}</ChatMessage>
                </div>
              ))}

              {state.step === "scoping" ? (
                <StatusLine>Scope 正在展开问题…（同步调用，约数十秒）</StatusLine>
              ) : null}

              {state.step === "clarify" ? (
                <ChatMessage from="scope">{state.clarificationQuestion}</ChatMessage>
              ) : null}

              {state.step === "brief" ? (
                <BriefCard
                  brief={state.brief}
                  tag={state.tag}
                  editing={state.editing}
                  reviseOpen={state.reviseOpen}
                  reviseNote={state.reviseNote}
                  onBriefTextChange={(text) =>
                    setState({ ...state, brief: { ...state.brief, brief_text: text } })
                  }
                  onReviseNoteChange={(note) => setState({ ...state, reviseNote: note })}
                  onRevise={() => void reviseBrief()}
                  onToggleRevise={() => setState({ ...state, reviseOpen: !state.reviseOpen })}
                  onToggleEdit={() => {
                    if (state.editing) toast("Brief 已保存到本地，确认时提交");
                    setState({ ...state, editing: !state.editing });
                  }}
                  onConfirm={() => void confirmBrief()}
                  onQuit={quitBrief}
                />
              ) : null}
            </div>
          )}
        </div>
      </div>

      {idle ? null : (
        <DockComposer
          draft={draft}
          onDraftChange={setDraft}
          active={state.step === "clarify"}
          busy={busy}
          canSend={canSend}
          placeholder={dockPlaceholder}
          error={error}
          onSend={sendClarify}
          inputRef={textareaRef}
        />
      )}
    </section>
  );
}
