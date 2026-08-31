import assert from "node:assert/strict";
import test from "node:test";

import { jobStatusLabel } from "../src/lib/labels.ts";
import { statusTone } from "../src/lib/status.ts";

test("完成任务不展示报告核验判定", () => {
  assert.equal(jobStatusLabel("completed", "report_rendered", "verified"), "已完成");
  assert.equal(jobStatusLabel("completed", "report_rendered", "partial"), "已完成");
  assert.equal(jobStatusLabel("completed", "report_rendered", "failed"), "已完成");
  assert.equal(statusTone("completed", "report_rendered", "verified"), "done");
  assert.equal(statusTone("completed", "report_rendered", "partial"), "done");
  assert.equal(statusTone("completed", "report_rendered", "failed"), "done");
});

test("旧任务也只展示完成状态", () => {
  assert.equal(jobStatusLabel("completed", "partial", null), "已完成");
  assert.equal(jobStatusLabel("completed", "draft_rendered", null), "已完成");
  assert.equal(jobStatusLabel("completed", "verified", null), "已完成");
  assert.equal(statusTone("completed", "partial", null), "done");
  assert.equal(statusTone("completed", "verified", null), "done");
});

test("未收尾的中间态统一显示为研究中", () => {
  assert.equal(jobStatusLabel("running", null, null), "研究中");
  assert.equal(jobStatusLabel("queued", null, null), "研究中");
  assert.equal(jobStatusLabel("cancelling", null, null), "研究中");
  assert.equal(statusTone("running", null, null), "running");
  assert.equal(statusTone("queued", null, null), "running");
  assert.equal(statusTone("failed", null, null), "danger");
});
