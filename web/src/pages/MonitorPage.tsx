import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { subscribeJobEvents } from "../api/sse";
import type { ServerEvent } from "../api/types";
import { MonitorHead } from "../components/monitor/MonitorHead";
import { PlanPanel } from "../components/monitor/PlanPanel";
import { TimelinePanel } from "../components/monitor/TimelinePanel";
import { UsagePanel, type UsageMetric } from "../components/monitor/UsagePanel";
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
  planPages,
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
  const { toast } = useToast();
  const [view, setView] = useState<JobViewState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [cancelPending, setCancelPending] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const viewRef = useRef<JobViewState | null>(null);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  // 事件之间保持同一个数组身份，否则每秒一次的时钟会把 PlanPanel 的 memo 打穿。
  // 必须放在下面那几处提前 return 之前——hook 顺序不能随渲染路径变。
  const pages = useMemo(() => (view ? planPages(view) : []), [view]);

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
    let replayComplete = false;
    let replayCursor = 0;
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
        viewRef.current = initial;
        // 概览来自详情快照，立即可用。SSE 历史只为重建时间线和计划轮次：在后台
        // 追平快照游标后一次性显示，避免逐条闪现；游标之后才是实时事件。
        replayCursor = detail.latest_event_id;
        replayComplete = replayCursor === 0;
        setHistoryLoading(!replayComplete);
        setView(initial);
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
              if (!replayComplete && event.id >= replayCursor) {
                replayComplete = true;
                setHistoryLoading(false);
              }
              if (replayComplete) setView(next);
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
    if (!jobId || !view || cancelPending) return;
    if (!window.confirm("确定取消这个研究任务吗？任务取消后不能继续。")) return;
    setCancelPending(true);
    try {
      await api.cancelJob(jobId);
      toast("取消请求已记录，将在安全边界停止");
    } catch (err) {
      setCancelPending(false);
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
  const cancellable = !cancelPending && (view.status === "running" || view.status === "queued");

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
    <section className="view monitor-view">
      <MonitorHead
        status={view.status}
        statusLabel={statusLabel(view)}
        phaseIndex={view.phaseIndex}
        onCancel={() => void cancel()}
        cancellable={cancellable}
      />

      {/* 整页上下滚：左栏想多长多长，右栏时间轴定高吸在视口里。它的高度只看
          视口，不参与任何剩余空间分配——上一版就是让它去分右栏剩下的高度，
          结果配额一变、翻一页计划，它就跟着变。 */}
      <div className="monitor-grid">
        <div className="monitor-main">
          {/* 配额在上：它高度固定。研究计划在下，运行中会不断长高，放上面会把
              配额一路往下推。 */}
          <UsagePanel metrics={usage} />
          <PlanPanel pages={pages} roundsLeft={limits.decisionRoundLimit - roundsUsed} />
        </div>
        <div className="monitor-rail">
          <TimelinePanel rows={view.timeline} historyLoading={historyLoading} />
        </div>
      </div>
    </section>
  );
}
