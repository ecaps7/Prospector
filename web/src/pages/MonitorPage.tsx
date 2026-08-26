import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
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
import { apiErrorLabel, jobStatusLabel } from "../lib/labels";
import { limitsForEffort, maxConcurrency } from "../state/budget";
import {
  elapsedSeconds,
  fold,
  fromSnapshot,
  isFinished,
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
  return jobStatusLabel(state.status);
}

export function MonitorPage() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [view, setView] = useState<JobViewState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const viewRef = useRef<JobViewState | null>(null);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  // 已运行时长要每秒往前走，但这是整页重绘的唯一理由：任务一结束就不该再跳。
  const finished = view !== null && isFinished(view);
  const ticking = view !== null && !finished;
  useEffect(() => {
    if (!ticking) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    let applyQueue: Promise<void> = Promise.resolve();
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
        if (!cancelled) setError(apiErrorLabel(err, "无法加载任务"));
        return;
      }

      unsubscribe = subscribeJobEvents(jobId, {
        onEvent: (event) => {
          applyQueue = applyQueue
            .then(async () => {
              if (cancelled) return;
              const current = viewRef.current;
              if (!current) return;
              const next = await applyEvent(event, current);
              if (cancelled) return;
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
      unsubscribe?.();
    };
  }, [jobId, reloadKey]);

  const cancel = async () => {
    if (!jobId || !view) return;
    try {
      await api.cancelJob(jobId);
      toast("取消请求已记录，将在安全边界停止");
    } catch (err) {
      toast(apiErrorLabel(err, "无法取消"));
    }
  };

  if (error)
    return (
      <ErrorView
        message={error}
        onRetry={() => {
          setError(null);
          setReloadKey((key) => key + 1);
        }}
      />
    );
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

  const usage: UsageMetric[] = [
    {
      name: "规划决策轮",
      value: `${roundsUsed} / ${limits.decisionRoundLimit}`,
      pct: limits.decisionRoundLimit ? (roundsUsed / limits.decisionRoundLimit) * 100 : 0,
      accent: true,
    },
    { name: "并发调查", value: `${conc} / ${concMax}`, pct: concMax ? (conc / concMax) * 100 : 0 },
    // 以下四项没有真实上限，不给进度条——否则会和上面两条真配额看起来一样。
    { name: "输入 Token", value: fmtNum(tokIn) },
    { name: "输出 Token", value: fmtNum(tokOut) },
    { name: "工具调用", value: String(tools) },
    { name: "已运行", value: fmtClock(elapsed) },
  ];

  return (
    <section className="view">
      <MonitorHead
        status={view.status}
        statusLabel={statusLabel(view)}
        question={view.question}
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
