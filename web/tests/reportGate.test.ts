import assert from "node:assert/strict";
import test from "node:test";

import type { JobDetail } from "../src/api/types.ts";
import { isGateLive, reportGate, reportGateCopy } from "../src/state/reportGate.ts";

function job(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    job_id: "j1",
    question: "问题",
    effort: "standard",
    status: "running",
    phase: "research",
    outcome: null,
    verification_status: null,
    error_code: null,
    created_at: "2026-08-26T10:00:00Z",
    updated_at: "2026-08-26T10:03:20Z",
    brief_id: "b1",
    language: "zh",
    plan_version: 1,
    latest_event_id: 12,
    tasks: [],
    usage: [],
    report: null,
    ...overrides,
  };
}

const ready = { report_id: "r1", status: "stored", verification_status: "verified", markdown_ref: "s3://md", json_ref: "s3://json" };

test("报告文件在了就是就绪，哪怕任务还没收尾", () => {
  assert.equal(reportGate(job({ report: ready })).kind, "ready");
  assert.equal(reportGate(job({ status: "running", phase: "writing", report: ready })).kind, "ready");
});

test("撰写之前和撰写之后分成两种等待", () => {
  assert.equal(reportGate(job({ phase: "research" })).kind, "researching");
  assert.equal(reportGate(job({ phase: "verifier" })).kind, "researching");
  assert.equal(reportGate(job({ status: "queued", phase: "queued" })).kind, "researching");
  assert.equal(reportGate(job({ phase: "composition_pending" })).kind, "composing");
  assert.equal(reportGate(job({ phase: "writing" })).kind, "composing");
  assert.equal(reportGate(job({ phase: "attributing" })).kind, "composing");
  assert.equal(reportGate(job({ phase: "revising" })).kind, "composing");
});

test("终态各说各的，不再共用一句尚未就绪", () => {
  assert.equal(reportGate(job({ status: "cancelling", phase: "research" })).kind, "cancelling");
  assert.equal(reportGate(job({ status: "cancelled", phase: "cancelled" })).kind, "cancelled");
  assert.equal(reportGate(job({ status: "failed", phase: "verifier" })).kind, "failed");
  // 跑完了却没有报告文件——这才是真的异常，不能说成"还在生成"。
  assert.equal(reportGate(job({ status: "completed", phase: "report_rendered" })).kind, "missing");
});

test("只有还会变的状态才值得继续等", () => {
  assert.equal(isGateLive("researching"), true);
  assert.equal(isGateLive("composing"), true);
  assert.equal(isGateLive("cancelling"), true);
  assert.equal(isGateLive("cancelled"), false);
  assert.equal(isGateLive("failed"), false);
  assert.equal(isGateLive("missing"), false);
  assert.equal(isGateLive("ready"), false);
});

test("失败态把真正的原因摆到主句上", () => {
  const copy = reportGateCopy(
    job({ status: "failed", phase: "verifier", error_code: "verifier_major_gap" }),
    "failed",
    200,
  );
  assert.equal(copy.title, "证据核对发现重大缺口");
  assert.match(copy.detail, /核验证据/);
});

test("阶段字段装的是状态时，不拼出「停在已取消阶段」这种话", () => {
  const copy = reportGateCopy(job({ status: "cancelled", phase: "cancelled" }), "cancelled", 200);
  assert.ok(!copy.detail.includes("停在"));
});

test("成文各步各有说法，不会拼出「正在修订次数用尽」", () => {
  assert.equal(reportGateCopy(job({ phase: "writing" }), "composing", 10).title, "正在撰写报告");
  assert.equal(
    reportGateCopy(job({ phase: "attributing" }), "composing", 10).title,
    "正文已写完，正在为每处表述寻找出处",
  );
  assert.equal(reportGateCopy(job({ phase: "reviewing" }), "composing", 10).title, "正在通读全文审阅");
});

test("综合到审阅这一整段都算正在生成报告，不再说还在查资料", () => {
  const phases = [
    "composition_pending",
    "synthesizing",
    "composition",
    "writing",
    "attributing",
    "reviewing",
    "revising",
    "rendering",
  ];
  for (const phase of phases) {
    assert.equal(reportGate(job({ phase })).kind, "composing", phase);
  }
});

test("等待中的辅助句带上已运行时长", () => {
  const copy = reportGateCopy(job({ phase: "research" }), "researching", 200);
  assert.match(copy.detail, /当前：搜集资料/);
  assert.match(copy.detail, /已运行 03:20/);
});
