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

test("report verifier start is a 核验 line", () => {
  assert.deepEqual(
    lines([event(1, "job.phase_changed", { phase: "verifying" })]),
    ["[核验] Report Verifier 正在逐句验证"],
  );
});

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

test("task start shows its concrete budget without a research stage", () => {
  assert.deepEqual(
    lines([
      event(1, "task.started", {
        task_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        question: "核验公开数据",
        budget: { max_worker_rounds: 48 },
      }),
    ]),
    ["[T1] 开始调查（调查轮次预算 48 轮）"],
  );
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

test("成文那一整段都出行，不再从核验直接跳到报告已生成", () => {
  assert.deepEqual(
    lines([
      event(1, "job.phase_changed", { phase: "composition_pending" }),
      event(2, "job.phase_changed", { phase: "synthesizing" }),
      event(3, "job.phase_changed", { phase: "composition" }),
      event(4, "job.phase_changed", { phase: "attributing" }),
      event(5, "job.phase_changed", { phase: "reviewing" }),
      event(6, "job.phase_changed", { phase: "partial" }),
      event(7, "job.phase_changed", { phase: "rendering" }),
    ]),
    [
      "[综合] 证据核对已放行，等待研究综合",
      "[综合] 正在整合研究材料",
      "[成文] 研究综合完成，开始写作",
      "[成文] 正在为正文寻找出处",
      "[成文] 正在通读全文审阅",
      "[成文] 部分内容未获事实支持，报告仍会交付",
      "[成文] 正在渲染最终报告",
    ],
  );
});

test("研究综合的结论进时间轴，补研究和放行分得开", () => {
  assert.deepEqual(
    lines([
      event(1, "synthesis.completed", {
        synthesis_run_id: "s1",
        decision: "ready",
        synthesis: "材料已经能够回应问题。后面还有别的话，不该跟着挤进这一行。",
      }),
      event(2, "synthesis.completed", {
        synthesis_run_id: "s2",
        decision: "needs_research",
        synthesis: "缺少监管口径的数据。",
      }),
    ]),
    [
      "[综合] 研究综合完成：材料已经能够回应问题。",
      "[综合] 研究综合请求补研究：缺少监管口径的数据。",
    ],
  );
});

test("没有句号的长结论按字数截断，时间轴的行仍然是一行", () => {
  const long = "甲".repeat(200);
  const [row] = lines([
    event(1, "synthesis.completed", { synthesis_run_id: "s1", decision: "ready", synthesis: long }),
  ]);
  assert.ok(row.endsWith("…"), row);
  assert.ok(row.length < 90, String(row.length));
});

test("交付只出一行，判定就写在这一行上", () => {
  assert.deepEqual(
    lines([
      event(1, "report.draft_rendered", { report_id: "r1", verification_status: "partial" }),
      event(2, "job.phase_changed", { phase: "report_rendered" }),
    ]),
    ["[成文] 报告已交付（部分核验）"],
  );
});

test("研究综合请求补研究时，核验行说得出这是第二次进核验", () => {
  assert.deepEqual(
    lines([
      event(1, "job.phase_changed", { phase: "verifier", plan_version: 2, trigger: "synthesis_gap" }),
    ]),
    ["[研究] 研究阶段结束，等待核验（研究计划 第 2 版，触发：研究综合请求补研究）"],
  );
});

test("反复检索却不落库的任务，收工原因说的是人话", () => {
  assert.deepEqual(
    lines([
      event(1, "task.finished", {
        task_id: "task-1",
        stop_reason: "repeating_without_progress",
        rounds_used: 9,
        rounds_limit: 20,
        tool_calls_used: 31,
        assertion_count: 2,
      }),
    ]),
    ["[T1] 收工：连续重复同一组检索且未落库（轮 9/20，工具 31 次，累计断言 2 条）"],
  );
});

test("成功收尾时 phase 和 outcome 同为 report_rendered，只说一次", () => {
  assert.deepEqual(
    lines([
      event(1, "job.stopped", {
        status: "completed",
        phase: "report_rendered",
        outcome: "report_rendered",
      }),
    ]),
    ["[结束] 已完成 · 报告已生成"],
  );
});

test("plan cards follow the canonical task order instead of planner array order", () => {
  const taskIds = ["task-1", "task-2", "task-3"];
  const tasks = taskIds.map((taskId) => ({
    taskId,
    question: taskId,
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

test("补充研究那一轮说得出自己的来由，不再复用一句「开始」", () => {
  assert.deepEqual(
    lines([
      event(1, "job.phase_changed", { phase: "research" }),
      event(2, "job.phase_changed", { phase: "research", trigger: "verifier_follow_up" }),
    ]),
    ["[研究] 开始", "[研究] 核验已放行，但指明仍缺证据，补充研究一轮"],
  );
});

test("翻页翻的是派发，不是决策轮——判定收尾的那一轮本来就没有计划", () => {
  const tasks = ["task-1", "task-2", "task-3"].map((taskId) => ({
    taskId,
    question: taskId,
  }));
  const state = {
    tasks,
    planVersion: 2,
    planReason: null,
    planRounds: [
      { round: 1, planVersion: 1, reason: "", taskIds: ["task-1", "task-2"] },
      // 第 2 轮是被核验交回的那次 finish，不产生计划，所以轮号从 1 跳到 3。
      { round: 3, planVersion: 2, reason: "", taskIds: ["task-3"] },
    ],
  } as JobViewState;

  assert.deepEqual(
    planPages(state).map((page) => [page.round, page.planVersion]),
    [
      [1, 1],
      [3, 2],
    ],
  );
});
