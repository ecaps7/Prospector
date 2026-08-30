import assert from "node:assert/strict";
import test from "node:test";

import { jobStatusLabel } from "../src/lib/labels.ts";
import { statusTone } from "../src/lib/status.ts";

test("改版后的成色来自交付判定，不再来自 outcome", () => {
  // outcome 现在恒为 report_rendered，成色只在 verification_status 上。
  assert.equal(jobStatusLabel("completed", "report_rendered", "verified"), "已完成");
  assert.equal(jobStatusLabel("completed", "report_rendered", "partial"), "已完成 · 部分核验");
  assert.equal(jobStatusLabel("completed", "report_rendered", "failed"), "已完成 · 未通过核验");
  assert.equal(statusTone("completed", "report_rendered", "verified"), "done");
  assert.equal(statusTone("completed", "report_rendered", "partial"), "warn");
  assert.equal(statusTone("completed", "report_rendered", "failed"), "warn");
});

test("改版前的旧任务没有判定字段，仍按 outcome 读出成色", () => {
  assert.equal(jobStatusLabel("completed", "partial", null), "部分完成");
  assert.equal(jobStatusLabel("completed", "draft_rendered", null), "已完成 · 未逐句核对");
  assert.equal(jobStatusLabel("completed", "verified", null), "已完成");
  assert.equal(statusTone("completed", "partial", null), "warn");
  assert.equal(statusTone("completed", "verified", null), "done");
});

test("没收尾的任务不看判定", () => {
  assert.equal(jobStatusLabel("running", null, null), "研究中");
  assert.equal(jobStatusLabel("cancelling", null, null), "正在取消");
  assert.equal(statusTone("running", null, null), "running");
  assert.equal(statusTone("failed", null, null), "danger");
});
