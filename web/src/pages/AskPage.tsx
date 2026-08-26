import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { EffortLevel } from "../api/types";
import {
  clearAskSession,
  isRestorable,
  loadAskSession,
  saveAskSession,
  type AskState,
  type ClarifyTurn,
} from "../state/askSession";
import { BriefCard } from "../components/ask/BriefCard";
import { ChatMessage } from "../components/ask/ChatMessage";
import { DockComposer } from "../components/ask/DockComposer";
import { ExampleQuestions } from "../components/ask/ExampleQuestions";
import { HeroComposer } from "../components/ask/HeroComposer";
import { useToast } from "../components/Toast";
import { StatusLine } from "../components/ui/Status";
import { apiErrorLabel } from "../lib/labels";

export function AskPage() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [restored] = useState(loadAskSession);
  const [draft, setDraft] = useState(restored.draft);
  const [effort, setEffort] = useState<EffortLevel>(restored.effort);
  const [language, setLanguage] = useState(restored.language);
  const [state, setState] = useState<AskState>(restored.state);
  const [error, setError] = useState<string | null>(null);
  const [revising, setRevising] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const idle = state.step === "compose";
  const busy = state.step === "scoping";

  useEffect(() => {
    saveAskSession({ draft, effort, language, state });
  }, [draft, effort, language, state]);

  useEffect(() => {
    if (isRestorable(restored.state)) toast("已恢复上次未确认的研究方案");
    // Mount-only: `restored` is read once and never changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const canSend = (idle || state.step === "clarify") && !busy && Boolean(draft.trim());

  /** The Scope call in flight, plus how to put the flow back if the user stops it. */
  const pending = useRef<{ controller: AbortController; revert: () => void } | null>(null);

  // Leaving the page shouldn't leave a request running with nobody left to receive it.
  useEffect(() => () => pending.current?.controller.abort(), []);

  const runScope = useCallback(
    async (
      question: string,
      turns: ClarifyTurn[],
      clarification: ClarifyTurn | null,
      revert: () => void,
    ): Promise<void> => {
      pending.current?.controller.abort();
      const controller = new AbortController();
      pending.current = { controller, revert };
      setError(null);
      setState({ step: "scoping", question, turns });
      try {
        const outcome = await api.scope(
          {
            question,
            effort,
            language,
            ...(clarification
              ? {
                  clarification_question: clarification.question,
                  clarification_answer: clarification.answer,
                }
              : {}),
          },
          controller.signal,
        );
        if (outcome.kind === "clarify") {
          setState({
            step: "clarify",
            question,
            clarificationQuestion: outcome.clarification_question,
            turns,
          });
        } else {
          setState({
            step: "brief",
            question,
            turns,
            brief: outcome.brief,
            tag: "pending",
            briefTextOriginal: outcome.brief.brief_text,
            reviseOpen: false,
            reviseNote: "",
          });
        }
      } catch (err) {
        // Stopping already put the flow back where it was; don't paint an error over it.
        if (controller.signal.aborted) return;
        revert();
        setError(apiErrorLabel(err, "问题展开服务暂时不可用，稍后再试一次"));
      } finally {
        if (pending.current?.controller === controller) pending.current = null;
      }
    },
    [effort, language],
  );

  /** Abandons the in-flight Scope call and hands the user their text back. */
  const stopScope = useCallback(() => {
    const current = pending.current;
    if (!current) return;
    pending.current = null;
    current.controller.abort();
    current.revert();
    setError(null);
    toast("已停止展开");
  }, [toast]);

  const submitQuestion = () => {
    const trimmed = draft.trim();
    if (!trimmed) {
      toast("请先输入一个研究问题");
      return;
    }
    setDraft("");
    void runScope(trimmed, [], null, () => {
      setState({ step: "compose" });
      setDraft(trimmed);
    });
  };

  const sendClarify = () => {
    if (state.step !== "clarify") return;
    const answer = draft.trim();
    if (!answer) {
      toast("请输入澄清回答");
      return;
    }
    const { question, clarificationQuestion } = state;
    const priorTurns = state.turns;
    const turn = { question: clarificationQuestion, answer };
    setDraft("");
    void runScope(question, [...priorTurns, turn], turn, () => {
      // Undo the send outright: re-asking the same question below a transcript that
      // already shows it answered just reads as a glitch.
      setState({ step: "clarify", question, clarificationQuestion, turns: priorTurns });
      setDraft(answer);
    });
  };

  const confirmBrief = useCallback(async () => {
    if (state.step !== "brief" || revising) return;
    try {
      const created = await api.createJob(state.brief);
      clearAskSession();
      toast("研究任务已创建");
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(apiErrorLabel(err, "无法创建任务，稍后再试一次"));
    }
  }, [navigate, revising, state, toast]);

  const reviseAbort = useRef<AbortController | null>(null);

  useEffect(() => () => reviseAbort.current?.abort(), []);

  const stopRevise = useCallback(() => {
    reviseAbort.current?.abort();
    reviseAbort.current = null;
    setRevising(false);
    setError(null);
  }, []);

  const reviseBrief = async () => {
    if (state.step !== "brief" || revising) return;
    const note = state.reviseNote.trim();
    if (!note) {
      toast("请输入一条修订指令");
      return;
    }
    setError(null);
    // Revise is the same slow Scope service, so it gets the same stop handle.
    const controller = new AbortController();
    reviseAbort.current = controller;
    setRevising(true);
    try {
      const { brief } = await api.reviseScope(
        {
          question: state.question,
          previous_brief: state.brief,
          revision_note: note,
          effort: state.brief.effort,
          language: state.brief.language,
        },
        controller.signal,
      );
      setState((current) =>
        current.step === "brief"
          ? {
              ...current,
              brief,
              tag: "revised",
              briefTextOriginal: brief.brief_text,
              reviseOpen: false,
              reviseNote: "",
            }
          : current,
      );
      toast("已按你的指令重写一版");
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(apiErrorLabel(err, "修订失败，稍后再试一次"));
    } finally {
      if (reviseAbort.current === controller) {
        reviseAbort.current = null;
        setRevising(false);
      }
    }
  };

  const quitBrief = useCallback(() => {
    if (state.step === "compose") return;
    // Discarding throws away a Scope run that cost the user half a minute, so keep
    // the whole flow around and let the toast put it back.
    const discarded = state;
    setDraft(state.question);
    setState({ step: "compose" });
    toast("已放弃，未创建任务", {
      label: "撤销",
      onAct: () => {
        setState(discarded);
        setDraft("");
      },
    });
  }, [state, toast]);

  // Scope blocks for tens of seconds. Esc is the way out — without it the only exit
  // was a page refresh, which nothing on screen tells you about.
  useEffect(() => {
    if (!busy && !revising) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (revising) stopRevise();
      else stopScope();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, revising, stopRevise, stopScope]);

  // Brief shortcuts. Both take a modifier or a dedicated key, so nothing here can fire
  // from ordinary typing — which is also why the old "press C twice" guard is gone: it
  // only ever defended against the bare letters that created the hazard.
  const reviseOpen = state.step === "brief" && state.reviseOpen;
  useEffect(() => {
    if (state.step !== "brief" || revising) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat) return;
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        void confirmBrief();
        return;
      }
      if (event.key !== "Escape") return;
      event.preventDefault();
      // Escape means "back out of the thing I just opened" before it means "discard".
      if (reviseOpen) {
        setState((current) => (current.step === "brief" ? { ...current, reviseOpen: false } : current));
      } else {
        quitBrief();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmBrief, quitBrief, reviseOpen, revising, state.step]);

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

  // The dock is only rendered while it can actually take input or be stopped; the brief
  // step owns its own controls, so it gets the height back instead of a disabled field.
  const showDock = state.step === "scoping" || state.step === "clarify";
  const dockPlaceholder = state.step === "clarify" ? "回答上方的澄清问题…" : "正在展开问题…";

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
                <StatusLine>正在展开你的问题…</StatusLine>
              ) : null}

              {state.step === "clarify" ? (
                <ChatMessage from="scope">{state.clarificationQuestion}</ChatMessage>
              ) : null}

              {state.step === "brief" ? (
                <BriefCard
                  brief={state.brief}
                  tag={state.tag}
                  briefTextOriginal={state.briefTextOriginal}
                  reviseOpen={state.reviseOpen}
                  reviseNote={state.reviseNote}
                  revising={revising}
                  onStopRevise={stopRevise}
                  onBriefTextChange={(text) =>
                    setState({ ...state, brief: { ...state.brief, brief_text: text } })
                  }
                  onRestoreBriefText={() =>
                    setState({
                      ...state,
                      brief: { ...state.brief, brief_text: state.briefTextOriginal },
                    })
                  }
                  onReviseNoteChange={(note) => setState({ ...state, reviseNote: note })}
                  onRevise={() => void reviseBrief()}
                  onToggleRevise={() => setState({ ...state, reviseOpen: !state.reviseOpen })}
                  onConfirm={() => void confirmBrief()}
                  onQuit={quitBrief}
                />
              ) : null}
            </div>
          )}
        </div>
      </div>

      {showDock ? (
        <DockComposer
          draft={draft}
          onDraftChange={setDraft}
          active={state.step === "clarify"}
          busy={busy}
          canSend={canSend}
          placeholder={dockPlaceholder}
          error={error}
          onSend={sendClarify}
          onStop={stopScope}
          inputRef={textareaRef}
        />
      ) : null}
    </section>
  );
}
