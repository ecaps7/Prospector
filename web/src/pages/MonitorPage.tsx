import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { subscribeJobEvents } from "../api/sse";
import type { ServerEvent } from "../api/types";
import { FinishCard } from "../components/monitor/FinishCard";
import { MonitorHead } from "../components/monitor/MonitorHead";
import { PlanPanel } from "../components/monitor/PlanPanel";
import { TimelinePanel } from "../components/monitor/TimelinePanel";
import { UsagePanel, type UsageMetric } from "../components/monitor/UsagePanel";
import { PhaseTrack } from "../components/monitor/PhaseTrack";
import { useToast } from "../components/Toast";
import { ErrorView, LoadingView } from "../components/ui/Status";
import { fmtClock, fmtNum } from "../lib/format";
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
      usage: view.usage,
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

  if (error) return <ErrorView message={error} />;
  if (!view) return <LoadingView>正在加载任务…</LoadingView>;

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
  const finished =
    view.stopped || view.status === "completed" || view.status === "failed" || view.status === "cancelled";

  const usage: UsageMetric[] = [
    {
      name: "Planner 决策轮",
      value: `${roundsUsed} / ${limits.decisionRoundLimit}`,
      pct: limits.decisionRoundLimit ? (roundsUsed / limits.decisionRoundLimit) * 100 : 0,
      accent: true,
    },
    { name: "并发 Worker", value: `${conc} / ${concMax}`, pct: concMax ? (conc / concMax) * 100 : 0 },
    { name: "Tokens 输入", value: fmtNum(tokIn), pct: tokIn ? Math.min(100, tokIn / 6000) : 0 },
    { name: "Tokens 输出", value: fmtNum(tokOut), pct: tokOut ? Math.min(100, tokOut / 2000) : 0 },
    { name: "工具调用", value: String(tools), pct: tools ? Math.min(100, tools) : 0 },
    { name: "已运行", value: fmtClock(elapsed), pct: Math.min(100, (elapsed / 3600) * 100) },
  ];

  return (
    <section className="view">
      <MonitorHead
        status={view.status}
        statusLabel={statusLabel(view)}
        question={view.question}
        speed={speed}
        onSpeedChange={setSpeed}
        onReplay={replay}
        canReplay={hasEvents}
        onCancel={() => void cancel()}
        cancellable={cancellable}
      />

      <PhaseTrack phaseIndex={view.phaseIndex} status={view.status} />

      <div className="monitor-grid">
        <PlanPanel
          planVersion={view.planVersion}
          planReason={view.planReason}
          roundsLeft={limits.decisionRoundLimit - roundsUsed}
          tasks={view.tasks}
        />
        <UsagePanel metrics={usage} />
      </div>

      <TimelinePanel rows={view.timeline} />

      {finished ? (
        <FinishCard
          status={view.status}
          phase={view.phase}
          outcome={view.outcome}
          errorCode={view.errorCode}
          elapsed={elapsed}
          tokens={tokIn + tokOut}
          tools={tools}
          onOpenReport={() => navigate(`/jobs/${view.jobId}/report`)}
        />
      ) : null}
    </section>
  );
}
