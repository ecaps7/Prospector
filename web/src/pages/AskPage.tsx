import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api, emptyConstraints } from "../api/client";
import type { EffortLevel, ResearchBrief, UserConstraints } from "../api/types";
import { useToast } from "../components/Toast";

type ClarifyTurn = {
  question: string;
  answer: string;
};

type AskState =
  | { step: "compose" }
  | { step: "scoping"; question: string }
  | {
      step: "clarify";
      question: string;
      clarificationQuestion: string;
      turns: ClarifyTurn[];
      draftAnswer: string;
    }
  | {
      step: "brief";
      question: string;
      brief: ResearchBrief;
      tag: "pending" | "revised";
      editing: boolean;
      reviseOpen: boolean;
      reviseNote: string;
    };

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

export function AskPage() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [question, setQuestion] = useState("");
  const [effort, setEffort] = useState<EffortLevel>("standard");
  const [language, setLanguage] = useState("zh");
  const [state, setState] = useState<AskState>({ step: "compose" });
  const [error, setError] = useState<string | null>(null);

  const runScope = useCallback(
    async (
      nextQuestion: string,
      clarification?: { question: string; answer: string },
    ): Promise<void> => {
      setError(null);
      setState({ step: "scoping", question: nextQuestion });
      try {
        const outcome = await api.scope({
          question: nextQuestion,
          effort,
          language,
          clarification_question: clarification?.question,
          clarification_answer: clarification?.answer,
        });
        if (outcome.kind === "clarify") {
          setState((current) => ({
            step: "clarify",
            question: nextQuestion,
            clarificationQuestion: outcome.clarification_question,
            turns: current.step === "clarify" ? current.turns : [],
            draftAnswer: "",
          }));
        } else {
          setState({
            step: "brief",
            question: nextQuestion,
            brief: outcome.brief,
            tag: "pending",
            editing: false,
            reviseOpen: false,
            reviseNote: "",
          });
        }
      } catch (err) {
        setState({ step: "compose" });
        setError(err instanceof ApiError ? err.message : "Scope 服务不可用");
      }
    },
    [effort, language],
  );

  const submitQuestion = () => {
    const trimmed = question.trim();
    if (!trimmed) {
      toast("请先输入一个研究问题");
      return;
    }
    void runScope(trimmed);
  };

  const sendClarify = () => {
    if (state.step !== "clarify") return;
    const answer = state.draftAnswer.trim();
    if (!answer) {
      toast("请输入澄清回答");
      return;
    }
    const nextTurns = [...state.turns, { question: state.clarificationQuestion, answer }];
    setState({ ...state, turns: nextTurns, draftAnswer: "" });
    void (async () => {
      setError(null);
      setState({ step: "scoping", question: state.question });
      try {
        const outcome = await api.scope({
          question: state.question,
          effort,
          language,
          clarification_question: state.clarificationQuestion,
          clarification_answer: answer,
        });
        if (outcome.kind === "clarify") {
          setState({
            step: "clarify",
            question: state.question,
            clarificationQuestion: outcome.clarification_question,
            turns: nextTurns,
            draftAnswer: "",
          });
        } else {
          setState({
            step: "brief",
            question: state.question,
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
          question: state.question,
          clarificationQuestion: state.clarificationQuestion,
          turns: nextTurns,
          draftAnswer: "",
        });
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
    setState({ step: "compose" });
    toast("已放弃，未创建任务");
  }, [toast]);

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

  const composerLocked = state.step !== "compose";

  return (
    <section className="view">
      <div className="ask-hero">
        <h1>提出一个需要多方查证的问题</h1>
        <p>
          Prospector 会自行拆解问题、检索并交叉核对，最终产出一篇每句话都能追溯到网页快照的长篇报告。核对不过关的句子会被如实标出。
        </p>
      </div>

      <div className="card composer" style={composerLocked ? { opacity: 0.55, pointerEvents: "none" } : undefined}>
        <textarea
          aria-label="研究问题"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="例如：一个需要多方来源交叉核对、而不是搜一下就能答的问题"
        />
        <div className="composer-bar">
          <span className="field-label">研究档位</span>
          <div className="seg" role="radiogroup" aria-label="研究档位">
            {(["quick", "standard", "deep"] as const).map((value) => (
              <button
                key={value}
                type="button"
                className={effort === value ? "on" : ""}
                onClick={() => setEffort(value)}
              >
                {value === "quick" ? "quick 快速" : value === "standard" ? "standard 标准" : "deep 深入"}
              </button>
            ))}
          </div>
          <span className="field-label">语言</span>
          <div className="seg" role="radiogroup" aria-label="报告语言">
            <button type="button" className={language === "zh" ? "on" : ""} onClick={() => setLanguage("zh")}>
              中文
            </button>
            <button type="button" className={language === "en" ? "on" : ""} onClick={() => setLanguage("en")}>
              English
            </button>
          </div>
          <div className="spacer" />
          <button className="btn primary" type="button" onClick={submitQuestion} disabled={state.step === "scoping"}>
            开始展开问题
          </button>
        </div>
      </div>

      <div className="flow">
        {error ? <p className="form-error">{error}</p> : null}
        {state.step === "scoping" ? (
          <div className="scope-status">
            <span className="spinner" />
            <span>Scope 正在展开问题…（同步调用，约数十秒）</span>
          </div>
        ) : null}

        {state.step === "clarify" ? (
          <>
            <div className="msg user">
              <span className="who">我</span>
              <div className="bubble">{state.question}</div>
            </div>
            {state.turns.map((turn, index) => (
              <div key={`${turn.question}-${index}`}>
                <div className="msg" style={{ marginTop: 12 }}>
                  <span className="who">S</span>
                  <div className="bubble">{turn.question}</div>
                </div>
                <div className="msg user" style={{ marginTop: 12 }}>
                  <span className="who">我</span>
                  <div className="bubble">{turn.answer}</div>
                </div>
              </div>
            ))}
            <div className="msg" style={{ marginTop: 12 }}>
              <span className="who">S</span>
              <div className="bubble">{state.clarificationQuestion}</div>
            </div>
            <div className="answer-row" style={{ marginTop: 12 }}>
              <input
                aria-label="澄清回答"
                value={state.draftAnswer}
                onChange={(event) => setState({ ...state, draftAnswer: event.target.value })}
                onKeyDown={(event) => {
                  if (event.key === "Enter") sendClarify();
                }}
              />
              <button className="btn primary sm" type="button" onClick={sendClarify}>
                回答
              </button>
            </div>
          </>
        ) : null}

        {state.step === "brief" ? (
          <div className="card brief-card">
            <div className="brief-head">
              <h3>Research Brief</h3>
              <span className={`tag ${state.tag === "pending" ? "warn" : "ok"}`}>
                {state.tag === "pending" ? "待确认" : "修订稿"}
              </span>
              <span className="chip soft mono">
                {state.brief.effort} · {state.brief.language}
              </span>
            </div>
            <div className="brief-body">
              <div className="brief-field">
                <div className="k">研究问题</div>
                <div className="v">{state.brief.question}</div>
              </div>
              <div className="brief-field">
                <div className="k">研究空间 · BRIEF_TEXT（Scope 展开，Planner 可取舍）</div>
                {state.editing ? (
                  <textarea
                    className="brief-text editing"
                    value={state.brief.brief_text}
                    onChange={(event) =>
                      setState({
                        ...state,
                        brief: { ...state.brief, brief_text: event.target.value },
                      })
                    }
                  />
                ) : (
                  <div className="brief-text">{state.brief.brief_text}</div>
                )}
              </div>
              <div className="brief-field">
                <div className="k">用户约束 · USER_CONSTRAINTS（用户亲口所述，不可违背）</div>
                {constraintsEmpty(state.brief.user_constraints ?? emptyConstraints()) ? (
                  <div className="constraint-empty">
                    时间范围 / 地区 / 对比对象 / 来源规则 / 排除项均为空 —— 用户未施加任何范围约束
                  </div>
                ) : (
                  <div className="constraint-list">
                    {constraintRows(state.brief.user_constraints).map((row) => (
                      <div className="row" key={row.key}>
                        <span className="k">{row.label}</span>
                        <span>{row.text}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div className={`revise-row${state.reviseOpen ? " open" : ""}`}>
              <input
                value={state.reviseNote}
                onChange={(event) => setState({ ...state, reviseNote: event.target.value })}
                placeholder="输入一条修订指令，例如：增加对早期讨论的考察"
              />
              <button className="btn ghost sm" type="button" onClick={() => void reviseBrief()}>
                修订
              </button>
            </div>
            <div className="brief-foot">
              <button className="btn primary" type="button" onClick={() => void confirmBrief()}>
                确认，开始研究
              </button>
              <kbd>C</kbd>
              <div className="spacer" />
              <div className="quiet-acts">
                <button type="button" onClick={() => setState({ ...state, reviseOpen: !state.reviseOpen })}>
                  指令修订 <kbd>I</kbd>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (state.editing) toast("Brief 已保存到本地，确认时提交");
                    setState({ ...state, editing: !state.editing });
                  }}
                >
                  {state.editing ? "保存编辑" : <>编辑 <kbd>E</kbd></>}
                </button>
                <button type="button" onClick={quitBrief}>
                  放弃 <kbd>Q</kbd>
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
