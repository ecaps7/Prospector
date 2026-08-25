import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { subscribeJobEvents } from "../api/sse";
import type { ServerEvent } from "../api/types";
import { PhaseTrack } from "../components/PhaseTrack";
import { useToast } from "../components/Toast";
import { limitsForEffort, maxConcurrency } from "../state/budget";
import {
  elapsedSeconds,
  fold,
  fromSnapshot,
  mergeSnapshot,
  runningTasks,
  shouldRefreshSnapshot,
  totalInputTokens,
  totalOutputTokens,
  totalToolCalls,
  type JobViewState,
} from "../state/jobView";

const STAGE_LABEL: Record<string, string> = {
  scout: "scout",
  deep_dive: "deep_dive",
  verify: "verify",
};

const MODE_LABEL: Record<string, string> = {
  factual: "事实核验",
  comparison: "对比",
  counterargument: "反证",
  risk_scan: "风险扫描",
  timeline: "时间线",
};

function fmtNum(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

function fmtClock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function statusLabel(state: JobViewState): string {
  if (state.connectionState === "reconnecting") return "重连中";
  if (state.status === "completed") return "研究完成";
  if (state.status === "failed") return "失败";
  if (state.status === "cancelled") return "已取消";
  if (state.status === "cancelling") return "正在取消…";
  if (state.status === "queued") return "排队中";
  if (state.status === "running") return "研究中";
  return state.status;
}

function statusDotClass(state: JobViewState): string {
  if (state.status === "completed") return "done";
  if (state.status === "failed") return "danger";
  if (state.status === "cancelled" || state.status === "cancelling") return "warn";
  if (state.status === "running") return "running";
  return "";
}

export function MonitorPage() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [view, setView] = useState<JobViewState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [speed, setSpeed] = useState(1);
  const [now, setNow] = useState(0);
  const [eventsJobId, setEventsJobId] = useState<string | null>(null);
  const eventsRef = useRef<ServerEvent[]>([]);
  const viewRef = useRef<JobViewState | null>(null);
  const replayTimer = useRef<number | null>(null);
  const live = useRef(true);
  const hasEvents = Boolean(jobId) && eventsJobId === jobId;

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    let applyQueue: Promise<void> = Promise.resolve();
    live.current = true;
    eventsRef.current = [];
    const applyEvent = async (event: ServerEvent, current: JobViewState) => {
      let next = current;
      if (shouldRefreshSnapshot(event)) {
        try {
          next = mergeSnapshot(next, await api.getJob(jobId));
        } catch {
          /* keep folding even if snapshot refresh fails */
        }
      }
      return fold(next, event);
    };

    void (async () => {
      try {
        const detail = await api.getJob(jobId);
        if (cancelled) return;
        const initial = fromSnapshot(detail);
        setView(initial);
        viewRef.current = initial;
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "无法加载任务");
        return;
      }

      unsubscribe = subscribeJobEvents(jobId, {
        onEvent: (event) => {
          eventsRef.current = [...eventsRef.current, event];
          setEventsJobId(jobId);
          applyQueue = applyQueue
            .then(async () => {
              if (!live.current || cancelled) return;
              const current = viewRef.current;
              if (!current) return;
              const next = await applyEvent(event, current);
              if (!live.current || cancelled) return;
              viewRef.current = next;
              setView(next);
            })
            .catch(() => undefined);
        },
        onStatus: (state, delay) => {
          setView((current) =>
            current
              ? {
                  ...current,
                  connectionState: state,
                  reconnectDelay: delay ?? null,
                }
              : current,
          );
        },
      });
      if (cancelled) unsubscribe();
    })();

    return () => {
      cancelled = true;
      live.current = false;
      unsubscribe?.();
      if (replayTimer.current) window.clearTimeout(replayTimer.current);
    };
  }, [jobId]);

  const replay = () => {
    if (!view || !eventsRef.current.length) {
      toast("还没有可回放的事件");
      return;
    }
    live.current = false;
    if (replayTimer.current) window.clearTimeout(replayTimer.current);
    const snapshot = fromSnapshot({
      ...view,
      job_id: view.jobId,
      question: view.question,
      effort: view.effort as "quick" | "standard" | "deep",
      status: "running",
      phase: "running",
      outcome: null,
      error_code: null,
      created_at: view.createdAt,
      updated_at: view.createdAt,
      brief_id: null,
      language: view.language,
      plan_version: 0,
      tasks: view.tasks.map((task) => ({
        task_id: task.taskId,
        question: task.question,
        subjects: task.subjects,
        research_stage: task.researchStage,
        research_mode: task.researchMode,
        status: "pending",
        stop_reason: null,
        budget: { max_worker_rounds: task.roundsLimit },
        tool_calls_used: 0,
        created_at: view.createdAt,
        started_at: null,
        finished_at: null,
      })),
      usage: [],
      report: null,
    });
    viewRef.current = snapshot;
    setView(snapshot);
    let index = 0;
    let current = snapshot;
    const events = eventsRef.current;
    const step = () => {
      if (index >= events.length) {
        live.current = true;
        return;
      }
      const event = events[index];
      current = fold(current, event);
      viewRef.current = current;
      setView(current);
      index += 1;
      const next = events[index];
      if (!next) {
        live.current = true;
        return;
      }
      const prevTime = event.created_at ? new Date(event.created_at).getTime() : 0;
      const nextTime = next.created_at ? new Date(next.created_at).getTime() : prevTime + 400;
      const wait = Math.min(2000, Math.max(80, (nextTime - prevTime) / speed));
      replayTimer.current = window.setTimeout(step, Number.isFinite(wait) ? wait : 400 / speed);
    };
    replayTimer.current = window.setTimeout(step, 400 / speed);
  };

  const cancel = async () => {
    if (!jobId || !view) return;
    try {
      await api.cancelJob(jobId);
      toast("取消请求已记录，将在安全边界停止");
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "无法取消");
    }
  };

  if (error) {
    return (
      <section className="view">
        <p className="form-error">{error}</p>
      </section>
    );
  }
  if (!view) {
    return (
      <section className="view">
        <div className="scope-status">
          <span className="spinner" />
          <span>正在加载任务…</span>
        </div>
      </section>
    );
  }

  const limits = limitsForEffort(view.effort);
  const currentStage = view.tasks.find((task) => task.status === "running")?.researchStage;
  const concMax = maxConcurrency(view.effort, currentStage);
  const conc = runningTasks(view);
  const tokIn = totalInputTokens(view);
  const tokOut = totalOutputTokens(view);
  const tools = totalToolCalls(view);
  const elapsed = elapsedSeconds(view, now);
  const roundsUsed = view.researchDecisionsUsed;
  const cancellable = view.status === "running" || view.status === "queued" || view.status === "cancelling";
  const finished = view.stopped || view.status === "completed" || view.status === "failed" || view.status === "cancelled";

  return (
    <section className="view">
      <div className="monitor-head">
        <div className="job-capsule">
          <span className={`status-dot ${statusDotClass(view)}`} />
          <span className="job-label">{statusLabel(view)}</span>
          <span className="job-q">{view.question}</span>
        </div>
        <div className="replay-ctrl">
          <div className="seg" role="radiogroup" aria-label="回放速度">
            {[1, 2, 4].map((value) => (
              <button
                key={value}
                type="button"
                className={speed === value ? "on" : ""}
                onClick={() => setSpeed(value)}
              >
                {value}×
              </button>
            ))}
          </div>
          <button className="btn ghost sm" type="button" onClick={replay} disabled={!hasEvents}>
            重新回放
          </button>
          <button className="btn quiet sm" type="button" onClick={() => void cancel()} disabled={!cancellable}>
            取消任务
          </button>
        </div>
      </div>

      <PhaseTrack phaseIndex={view.phaseIndex} status={view.status} />

      <div className="monitor-grid">
        <div className="card plan-panel">
          <div className="plan-head">
            <span className="ver">Plan v{view.planVersion}</span>
            {view.planVersion > 1 ? <span className="tag replan">Replan</span> : null}
            <span className="chip soft mono">余 {Math.max(0, limits.decisionRoundLimit - roundsUsed)} 轮</span>
          </div>
          <p className="plan-note">{view.planReason || "等待 Planner 首个决策…"}</p>
          <div>
            {view.tasks.length === 0 ? <p className="muted">尚未派发任务</p> : null}
            {view.tasks.map((task, index) => {
              const rowClass =
                task.status === "running" ? "running" : task.status === "pending" ? "queued" : "";
              return (
                <div className={`task-row ${rowClass}`} key={task.taskId}>
                  <span className="task-idx">T{index + 1}</span>
                  <div className="task-main">
                    <div className="task-q">{task.question}</div>
                    <div className="task-meta">
                      <span className="tag neutral">{STAGE_LABEL[task.researchStage] ?? task.researchStage}</span>
                      <span className="tag neutral">{MODE_LABEL[task.researchMode] ?? task.researchMode}</span>
                    </div>
                  </div>
                  {task.status === "done" ? (
                    <span className="task-status">
                      <span className="tag ok">✔ 收工</span>
                    </span>
                  ) : null}
                  {task.status === "failed" ? (
                    <span className="task-status">
                      <span className="tag danger">失败</span>
                    </span>
                  ) : null}
                  {task.status === "running" ? (
                    <span className="task-status">
                      <span className="rounds-bar">
                        <span className="track">
                          <span
                            className="fill"
                            style={{
                              width: `${task.roundsLimit ? (task.roundsUsed / task.roundsLimit) * 100 : 0}%`,
                            }}
                          />
                        </span>
                        {task.roundsUsed}/{task.roundsLimit} 轮
                      </span>
                    </span>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
        <div className="card usage-panel">
          <div className="panel-title">限额与用量</div>
          <UsageRow
            accent
            name="Planner 决策轮"
            value={`${roundsUsed} / ${limits.decisionRoundLimit}`}
            pct={limits.decisionRoundLimit ? (roundsUsed / limits.decisionRoundLimit) * 100 : 0}
          />
          <UsageRow name="并发 Worker" value={`${conc} / ${concMax}`} pct={concMax ? (conc / concMax) * 100 : 0} />
          <UsageRow name="Tokens 输入" value={fmtNum(tokIn)} pct={tokIn ? Math.min(100, tokIn / 6000) : 0} />
          <UsageRow name="Tokens 输出" value={fmtNum(tokOut)} pct={tokOut ? Math.min(100, tokOut / 2000) : 0} />
          <UsageRow name="工具调用" value={String(tools)} pct={tools ? Math.min(100, tools) : 0} />
          <UsageRow name="已运行" value={fmtClock(elapsed)} pct={Math.min(100, (elapsed / 3600) * 100)} />
          <p className="usage-note">
            Token 与工具栏为相对展示，不构成硬上限。停止研究仍须经过 Research Verifier。
          </p>
        </div>
      </div>

      <div className="card timeline-panel">
        <div className="panel-title">事件时间线</div>
        <div className="timeline">
          {view.timeline.length === 0 ? <p className="muted">等待事件…</p> : null}
          {view.timeline.map((row, index) => (
            <div className={`tl-row ${row.cls}`} key={`${row.createdAt}-${index}-${row.text}`}>
              <span className="tl-time">{row.createdAt}</span>
              <span className="tl-tag">{row.tag}</span>
              <span className="tl-text">{row.text}</span>
            </div>
          ))}
        </div>
      </div>

      {finished ? (
        <div className="card finish-card">
          <h3>
            <span className={view.status === "completed" ? "ok-dot" : view.status === "failed" ? "fail-dot" : "warn-dot"}>
              {view.status === "completed" ? "✓" : view.status === "failed" ? "!" : "⊘"}
            </span>
            {view.status === "completed"
              ? `研究完成 · ${fmtClock(elapsed)}`
              : view.status === "failed"
                ? `研究失败${view.errorCode ? ` · ${view.errorCode}` : ""}`
                : "任务已取消"}
          </h3>
          <div className="finish-rows">
            <div className="item">
              <div className="k">阶段</div>
              <div className="v">{view.phase}</div>
            </div>
            <div className="item">
              <div className="k">结果</div>
              <div className="v">{view.outcome ?? view.status}</div>
            </div>
            <div className="item">
              <div className="k">用量</div>
              <div className="v">
                {fmtNum(tokIn + tokOut)} token · {tools} 次工具
              </div>
            </div>
          </div>
          <div className="acts">
            {view.status === "completed" ? (
              <button className="btn primary" type="button" onClick={() => navigate(`/jobs/${view.jobId}/report`)}>
                查看报告
              </button>
            ) : null}
            <Link className="btn ghost" to="/jobs">
              任务历史
            </Link>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function UsageRow({
  name,
  value,
  pct,
  accent,
}: {
  name: string;
  value: string;
  pct: number;
  accent?: boolean;
}) {
  return (
    <div className={`usage-row${accent ? " accent" : ""}`}>
      <div className="top">
        <span className="name">{name}</span>
        <span className="val">{value}</span>
      </div>
      <div className="track">
        <div className="fill" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </div>
    </div>
  );
}
