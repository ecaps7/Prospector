import type { JobDetail, JobTaskView, ServerEvent, UsageView } from "../api/types";
import { PHASE_STEPS } from "../lib/labels";
import { renderEvent, type TimelineContext, type TimelineEntry } from "./timeline";
import { appendTimelineEntries, timelineClock } from "./timelineDisplay";

export type ViewTask = {
  taskId: string;
  question: string;
  subjects: string[];
  researchStage: string;
  researchMode: string;
  status: string;
  stopReason: string | null;
  roundsUsed: number;
  roundsLimit: number;
  toolCallsUsed: number;
  seenToolCallIds: string[];
};

/** 一次 planner 派发 = 一页研究计划。任务表本身不记轮次，只有 planner.decided
 *  事件带着 task_ids，所以分页只能在客户端按事件重建。 */
export type PlanRound = {
  round: number;
  planVersion: number;
  reason: string;
  taskIds: string[];
};

export type JobViewState = {
  jobId: string;
  question: string;
  effort: string;
  language: string;
  status: string;
  phase: string;
  outcome: string | null;
  errorCode: string | null;
  createdAt: string;
  updatedAt: string;
  planVersion: number;
  planReason: string | null;
  planRounds: PlanRound[];
  tasks: ViewTask[];
  usage: UsageView[];
  connectionState: "connected" | "reconnecting";
  reconnectDelay: number | null;
  timeline: TimelineEntry[];
  stopped: boolean;
  lastEventId: number;
  phaseIndex: number;
  researchDecisionsUsed: number;
};

const PHASE_INDEX: Record<string, number> = {
  initialize: 0,
  queued: 0,
  running: 0,
  research: 2,
  verifier: 3,
  composition_pending: 4,
  writing: 4,
  verifying: 5,
  revising: 5,
  verified: 5,
  revisions_exhausted: 5,
  rendering: 6,
  draft_rendered: 6,
  cancelling: 0,
  cancelled: 0,
};

export const PHASE_LABELS = PHASE_STEPS;

export function phaseIndex(phase: string): number {
  return PHASE_INDEX[phase] ?? 0;
}

function taskFromSnapshot(task: JobTaskView): ViewTask {
  return {
    taskId: task.task_id,
    question: task.question,
    subjects: [...task.subjects],
    researchStage: task.research_stage,
    researchMode: task.research_mode,
    status: task.status,
    stopReason: task.stop_reason,
    roundsUsed: 0,
    roundsLimit: Number(task.budget?.max_worker_rounds ?? 0),
    toolCallsUsed: task.tool_calls_used,
    seenToolCallIds: [],
  };
}

function timelineContext(state: JobViewState): TimelineContext {
  return {
    effort: state.effort,
    taskQuestions: Object.fromEntries(state.tasks.map((task) => [task.taskId, task.question])),
    taskOrder: state.tasks.map((task) => task.taskId),
    researchDecisionsUsed: state.researchDecisionsUsed,
  };
}

export function fromSnapshot(detail: JobDetail): JobViewState {
  return {
    jobId: detail.job_id,
    question: detail.question || "（未命名研究）",
    effort: detail.effort ?? "standard",
    language: detail.language || "unknown",
    status: detail.status,
    phase: detail.phase,
    outcome: detail.outcome,
    errorCode: detail.error_code,
    createdAt: detail.created_at,
    updatedAt: detail.updated_at,
    planVersion: detail.plan_version,
    planReason: null,
    planRounds: [],
    tasks: detail.tasks.map(taskFromSnapshot),
    usage: [...detail.usage],
    connectionState: "connected",
    reconnectDelay: null,
    timeline: [],
    stopped: detail.status === "completed" || detail.status === "failed" || detail.status === "cancelled",
    lastEventId: 0,
    phaseIndex: phaseIndex(detail.phase),
    researchDecisionsUsed: 0,
  };
}

export function mergeSnapshot(state: JobViewState, detail: JobDetail): JobViewState {
  const tasks = [...state.tasks];
  for (const snapshot of detail.tasks) {
    const index = tasks.findIndex((task) => task.taskId === snapshot.task_id);
    if (index < 0) {
      tasks.push(taskFromSnapshot(snapshot));
      continue;
    }
    const current = tasks[index];
    tasks[index] = {
      ...current,
      question: snapshot.question,
      subjects: [...snapshot.subjects],
      researchStage: snapshot.research_stage,
      researchMode: snapshot.research_mode,
      stopReason: snapshot.stop_reason,
      roundsLimit: Number(snapshot.budget?.max_worker_rounds ?? current.roundsLimit),
      toolCallsUsed: Math.max(current.toolCallsUsed, snapshot.tool_calls_used),
      status:
        snapshot.status === "done" ||
        snapshot.status === "failed" ||
        snapshot.status === "cancelled" ||
        current.status === "pending"
          ? snapshot.status
          : current.status,
    };
  }
  return {
    ...state,
    question: detail.question || state.question,
    status: detail.status,
    phase: detail.phase,
    outcome: detail.outcome,
    errorCode: detail.error_code,
    updatedAt: detail.updated_at,
    planVersion: detail.plan_version,
    usage: [...detail.usage],
    phaseIndex: Math.max(state.phaseIndex, phaseIndex(detail.phase)),
    tasks,
    stopped:
      state.stopped ||
      detail.status === "completed" ||
      detail.status === "failed" ||
      detail.status === "cancelled",
  };
}

function getTask(state: JobViewState, event: ServerEvent): ViewTask | null {
  const raw = (event.payload.task_id as string | undefined) ?? event.task_id;
  if (!raw) return null;
  return state.tasks.find((task) => task.taskId === raw) ?? null;
}

function replaceTask(state: JobViewState, next: ViewTask): JobViewState {
  return {
    ...state,
    tasks: state.tasks.map((task) => (task.taskId === next.taskId ? next : task)),
  };
}

export function fold(state: JobViewState, event: ServerEvent): JobViewState {
  if (event.id <= state.lastEventId) return state;
  let next: JobViewState = { ...state, lastEventId: event.id };
  const payload = event.payload;
  const eventType = event.event_type;

  if (eventType === "job.phase_changed") {
    const phase = String(payload.phase ?? next.phase);
    next = {
      ...next,
      phase,
      phaseIndex: Math.max(next.phaseIndex, phaseIndex(phase)),
      outcome: payload.outcome == null ? next.outcome : String(payload.outcome),
      errorCode: payload.error_code == null ? next.errorCode : String(payload.error_code),
    };
  } else if (eventType === "planner.decided") {
    next = {
      ...next,
      planVersion: Number(payload.plan_version ?? next.planVersion),
      planReason: String(payload.reason ?? payload.note ?? next.planReason ?? "") || next.planReason,
      phaseIndex: Math.max(next.phaseIndex, 1),
      researchDecisionsUsed: Number(payload.research_decisions_used ?? next.researchDecisionsUsed + 1),
    };
    if (payload.decision === "dispatch") {
      const taskIds = ((payload.task_ids as string[] | undefined) ?? []).map(String);
      const round = Number(payload.decision_round ?? event.decision_round ?? next.planRounds.length + 1);
      const entry: PlanRound = {
        round,
        planVersion: Number(payload.plan_version ?? next.planVersion),
        reason: String(payload.reason ?? ""),
        taskIds,
      };
      // 同一轮只会派发一次，但重连补发时同号事件可能再来一遍——按轮号覆盖而不是追加。
      next = {
        ...next,
        planRounds: [...next.planRounds.filter((item) => item.round !== round), entry].sort(
          (a, b) => a.round - b.round,
        ),
      };
    }
  } else if (eventType === "replan.triggered") {
    next = { ...next, planVersion: Number(payload.plan_version ?? next.planVersion) };
  } else if (eventType === "task.started") {
    const task = getTask(next, event);
    if (task) {
      const budget = (payload.budget as { max_worker_rounds?: number } | undefined) ?? {};
      next = replaceTask(next, {
        ...task,
        status: "running",
        roundsLimit: Number(budget.max_worker_rounds ?? task.roundsLimit),
      });
    }
    next = { ...next, phaseIndex: Math.max(next.phaseIndex, 2) };
  } else if (eventType === "task.tool_used") {
    const task = getTask(next, event);
    const callId = payload.tool_call_id == null ? null : String(payload.tool_call_id);
    if (task && callId && !task.seenToolCallIds.includes(callId)) {
      next = replaceTask(next, {
        ...task,
        seenToolCallIds: [...task.seenToolCallIds, callId],
        toolCallsUsed: task.toolCallsUsed + 1,
      });
    }
  } else if (eventType === "task.round_advanced") {
    const task = getTask(next, event);
    if (task) {
      next = replaceTask(next, {
        ...task,
        roundsUsed: Number(payload.rounds_used ?? task.roundsUsed),
        roundsLimit: Number(payload.rounds_limit ?? task.roundsLimit),
      });
    }
  } else if (eventType === "task.finished") {
    const task = getTask(next, event);
    if (task) {
      next = replaceTask(next, {
        ...task,
        status: payload.error || payload.stop_reason === "tool_error" ? "failed" : "done",
        stopReason: payload.stop_reason == null ? task.stopReason : String(payload.stop_reason),
        roundsUsed: Number(payload.rounds_used ?? 0),
        roundsLimit: Number(payload.rounds_limit ?? task.roundsLimit),
        toolCallsUsed: Number(payload.tool_calls_used ?? task.toolCallsUsed),
      });
    }
  } else if (eventType === "verifier.completed") {
    next = { ...next, phaseIndex: Math.max(next.phaseIndex, 3) };
  } else if (eventType === "report.draft_rendered") {
    next = { ...next, phaseIndex: Math.max(next.phaseIndex, 6) };
  } else if (eventType === "job.stopped") {
    next = {
      ...next,
      status: String(payload.status ?? next.status),
      phase: String(payload.phase ?? next.phase),
      outcome: payload.outcome == null ? next.outcome : String(payload.outcome),
      errorCode: payload.error_code == null ? next.errorCode : String(payload.error_code),
      stopped: true,
    };
  }

  const ctx = timelineContext(next);
  const entries = renderEvent(ctx, event).map((entry) => ({
    ...entry,
    createdAt: timelineClock(event.created_at),
  }));
  next = {
    ...next,
    researchDecisionsUsed: ctx.researchDecisionsUsed,
    timeline: appendTimelineEntries(next.timeline, entries),
  };
  return next;
}

export function totalTokens(state: JobViewState): number {
  return state.usage.reduce((sum, item) => sum + item.input_tokens + item.output_tokens, 0);
}

export function totalInputTokens(state: JobViewState): number {
  return state.usage.reduce((sum, item) => sum + item.input_tokens, 0);
}

export function totalOutputTokens(state: JobViewState): number {
  return state.usage.reduce((sum, item) => sum + item.output_tokens, 0);
}

export function totalToolCalls(state: JobViewState): number {
  const fromUsage = state.usage.reduce((sum, item) => sum + item.tool_calls, 0);
  const fromTasks = state.tasks.reduce((sum, task) => sum + task.toolCallsUsed, 0);
  return Math.max(fromUsage, fromTasks);
}

export function runningTasks(state: JobViewState): number {
  return state.tasks.filter((task) => task.status === "running").length;
}

/**
 * True once the job can no longer change on its own. The elapsed clock freezes here,
 * and the monitor page stops ticking — a finished job has nothing left to animate.
 */
export function isFinished(state: JobViewState): boolean {
  return (
    state.stopped ||
    state.status === "completed" ||
    state.status === "failed" ||
    state.status === "cancelled"
  );
}

export function elapsedSeconds(state: JobViewState, now = Date.now()): number {
  const start = new Date(state.createdAt).getTime();
  const end = state.stopped ? new Date(state.updatedAt).getTime() : now;
  return Math.max(0, Math.floor((end - start) / 1000));
}

export type PlanPageTask = { task: ViewTask; index: number };

export type PlanPage = {
  round: number;
  planVersion: number;
  reason: string | null;
  tasks: PlanPageTask[];
};

/** 把任务按派发轮次切成一页一页。index 用任务在 state.tasks 里的全局序号，
 *  这样卡片上的 T1/T2 和时间轴上的标号始终是同一套编号。 */
export function planPages(state: JobViewState): PlanPage[] {
  const order = new Map(state.tasks.map((task, index) => [task.taskId, index]));
  const entry = (task: ViewTask): PlanPageTask => ({ task, index: order.get(task.taskId) ?? 0 });

  if (state.planRounds.length === 0) {
    return [
      {
        round: 0,
        planVersion: state.planVersion,
        reason: state.planReason,
        tasks: state.tasks.map(entry),
      },
    ];
  }

  const byId = new Map(state.tasks.map((task) => [task.taskId, task]));
  const claimed = new Set<string>();
  const pages = state.planRounds.map((round) => {
    const tasks: PlanPageTask[] = [];
    for (const taskId of round.taskIds) {
      const task = byId.get(taskId);
      if (!task) continue;
      claimed.add(taskId);
      tasks.push(entry(task));
    }
    tasks.sort((left, right) => left.index - right.index);
    return {
      round: round.round,
      planVersion: round.planVersion,
      reason: round.reason || null,
      tasks,
    };
  });

  // 快照可能先于事件到达，落下没被任何一轮认领的任务——挂到最后一页，别让它们消失。
  const orphans = state.tasks.filter((task) => !claimed.has(task.taskId)).map(entry);
  if (orphans.length) pages[pages.length - 1].tasks.push(...orphans);
  return pages;
}

export function shouldRefreshSnapshot(event: ServerEvent): boolean {
  return (
    event.event_type === "job.phase_changed" ||
    event.event_type === "task.finished" ||
    event.event_type === "job.stopped" ||
    (event.event_type === "planner.decided" && event.payload.decision === "dispatch")
  );
}
