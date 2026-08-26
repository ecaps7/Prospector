import assert from "node:assert/strict";
import test from "node:test";

import type { ServerEvent } from "../src/api/types.ts";
import { planPages, type JobViewState } from "../src/state/jobView.ts";
import { renderEvent, type TimelineContext } from "../src/state/timeline.ts";
import { filterTimelineEntries } from "../src/state/timelineDisplay.ts";

function ctx(): TimelineContext {
  return { effort: "standard", taskQuestions: {}, taskOrder: [], researchDecisionsUsed: 0 };
}

function event(id: number, eventType: string, payload: Record<string, unknown>): ServerEvent {
  return {
    id,
    event_type: eventType,
    payload,
    task_id: null,
    decision_round: null,
    created_at: "2026-08-26T17:00:15",
  };
}

function lines(events: ServerEvent[]): string[] {
  const context = ctx();
  return events.flatMap((item) =>
    renderEvent(context, item).map((row) => `[${row.tag}] ${row.text}`),
  );
}

test("cancelling a job reports 已取消 exactly once", () => {
  assert.deepEqual(
    lines([
      event(1, "job.phase_changed", { phase: "cancelling", requested_via: "web_monitor" }),
      event(2, "job.phase_changed", { phase: "cancelled", outcome: "cancelled" }),
      event(3, "job.stopped", { status: "cancelled", phase: "cancelled", outcome: "cancelled" }),
    ]),
    ["[研究] 正在取消", "[结束] 已取消"],
  );
});

test("a failed job states its reason on the single terminal line", () => {
  assert.deepEqual(
    lines([
      event(1, "job.phase_changed", {
        phase: "failed",
        outcome: "failed",
        error_code: "verifier_major_gap",
      }),
      event(2, "job.stopped", {
        status: "failed",
        phase: "failed",
        outcome: "failed",
        error_code: "verifier_major_gap",
      }),
    ]),
    ["[结束] 失败：证据核对发现重大缺口"],
  );
});

test("web_fetch 403 stays a tool event and is hidden from the key view", () => {
  const rows = renderEvent(
    ctx(),
    event(1, "task.tool_used", {
      task_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      tool: "web_fetch",
      error: "HTTP Error 403: Forbidden",
    }),
  );
  assert.equal(rows[0]?.cls, "tool");
  assert.match(rows[0]?.text ?? "", /web_fetch 失败：HTTP Error 403: Forbidden/);
  assert.equal(filterTimelineEntries(rows, "key").length, 0);
  assert.equal(filterTimelineEntries(rows, "all").length, 1);
});

test("verifier rejection remains a key gap, unlike tool fetch noise", () => {
  const rows = renderEvent(
    ctx(),
    event(1, "verifier.completed", {
      plan_version: 1,
      release_decision: "replan",
      major_gap_count: 2,
      research_decisions_used: 3,
    }),
  );
  assert.equal(rows[0]?.cls, "gap");
  assert.equal(filterTimelineEntries(rows, "key").length, 1);
});

test("a completed job keeps status, phase and outcome apart", () => {
  assert.deepEqual(
    lines([
      event(1, "job.stopped", {
        status: "completed",
        phase: "draft_rendered",
        outcome: "verified",
      }),
    ]),
    ["[结束] 已完成 · 报告已生成 · 已逐句核对"],
  );
});

test("plan cards follow the canonical task order instead of planner array order", () => {
  const taskIds = ["task-1", "task-2", "task-3"];
  const tasks = taskIds.map((taskId) => ({
    taskId,
    question: taskId,
    subjects: ["subject"],
    researchStage: "scout",
    researchMode: "factual",
    status: "pending",
    stopReason: null,
    roundsUsed: 0,
    roundsLimit: 2,
    toolCallsUsed: 0,
    seenToolCallIds: [],
  }));
  const state = {
    tasks,
    planVersion: 1,
    planReason: null,
    planRounds: [
      {
        round: 1,
        planVersion: 1,
        reason: "",
        taskIds: ["task-3", "task-1", "task-2"],
      },
    ],
  } as JobViewState;

  assert.deepEqual(
    planPages(state)[0]?.tasks.map(({ task, index }) => [`T${index + 1}`, task.taskId]),
    [
      ["T1", "task-1"],
      ["T2", "task-2"],
      ["T3", "task-3"],
    ],
  );
});
