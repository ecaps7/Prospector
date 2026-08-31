import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

const constraints = {
  time_range: "2024", regions: ["中国"], comparison_targets: [],
  source_rules: ["年度报告"], exclusions: ["医疗"], deliverable_rules: [],
};
const brief = {
  question: "年度收入变化", brief_text: "比较年度变化", effort: "quick", language: "zh",
  output_format: "report_with_citations", user_constraints: constraints,
};
const job = (id: string) => ({
  job_id: id, brief_id: "brief", question: brief.question, effort: "quick", language: "zh",
  status: "running", phase: "report_rendered", outcome: "report_rendered", error_code: null,
  created_at: "2026-08-31T00:00:00Z", updated_at: "2026-08-31T00:00:00Z",
  plan_version: 1, latest_event_id: 1, tasks: [], usage: [], verification_status: "failed",
  report: { report_id: "report", verification_status: "failed", status: "report_rendered",
    markdown_ref: "s3://test/report.md", json_ref: "s3://test/report.json" },
});
const markdown = (title: string) => `> **核对情况**：内部统计

# ${title}

第一版[^1]，第二版[^2]。

<img src=x onerror="alert(1)">
[危险链接](javascript:alert(1))

## 来源

[^1]: 年报，https://source.test/report（版本 1）
[^2]: 年报，https://source.test/report（版本 2）`;
const evidence = (version: number) => ({
  claim_id: `c${version}`, source_caveats: [],
  excerpt: { excerpt_id: `e${version}`, text: `第${version}版独有原文`,
    source: { source_uri: "https://source.test/report", document_version: version, title: "年报" } },
});
const audit = {
  verification_status: "failed", blocking_findings: [{ reason: "AUDIT_ONLY_FAILURE" }],
  claim_evidence: [evidence(1), evidence(2)],
};

async function health(page: Page) {
  await page.route(url => url.pathname.startsWith("/api/"), route => {
    throw new Error(`Unexpected API: ${route.request().url()}`);
  });
  await page.route("**/api/healthz", route => route.fulfill({ json: { status: "ok" } }));
}

test("报告已落库但任务未收尾时可阅读；角标按网址和版本打开原文", async ({ page }) => {
  await health(page);
  await page.route("**/api/jobs/a", route => route.fulfill({ json: job("a") }));
  let release!: () => void;
  const delay = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/jobs/a/report?format=md", route => route.fulfill({ body: markdown("报告A") }));
  await page.route("**/api/jobs/a/report?format=json", async route => {
    await delay;
    await route.fulfill({ json: audit });
  });
  await page.goto("/jobs/a/report");
  await expect(page.getByRole("heading", { name: "报告A", exact: true })).toBeVisible();
  await expect(page.getByText("内部统计")).toHaveCount(0);
  await expect(page.locator(".report-main img")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "危险链接" })).toHaveCount(0);
  release();
  await page.locator("button[data-cite='2']").click();
  const drawer = page.getByRole("dialog", { name: "证据摘录" });
  await expect(drawer.getByText("第2版独有原文")).toBeVisible();
  await expect(drawer.getByText("第1版独有原文")).toHaveCount(0);
  await expect(page.getByText("AUDIT_ONLY_FAILURE")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
});

test("切换任务后迟到的旧审计响应不污染新报告", async ({ page }) => {
  await health(page);
  const oldRequestSettled = new Promise<void>(resolve => {
    const finish = (request: { url(): string }) => {
      if (request.url().endsWith("/api/jobs/a/report?format=json")) resolve();
    };
    page.on("requestfinished", finish);
    page.on("requestfailed", finish);
  });
  for (const id of ["a", "b"]) {
    await page.route(`**/api/jobs/${id}`, route => route.fulfill({ json: job(id) }));
    await page.route(`**/api/jobs/${id}/report?format=md`, route => route.fulfill({ body: markdown(`报告${id}`) }));
  }
  let release!: () => void;
  const delay = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/jobs/a/report?format=json", async route => {
    await delay;
    await route.fulfill({ json: audit });
  });
  await page.route("**/api/jobs/b/report?format=json", route => route.fulfill({
    json: { ...audit, claim_evidence: [{ ...evidence(2), excerpt: {
      ...evidence(2).excerpt, text: "B任务原文",
    } }] },
  }));
  await page.goto("/jobs/a/report");
  await expect(page.getByRole("heading", { name: "报告a", exact: true })).toBeVisible();
  await page.evaluate(() => {
    history.pushState({}, "", "/jobs/b/report");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("heading", { name: "报告b", exact: true })).toBeVisible();
  release();
  await oldRequestSettled;
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await page.locator("button[data-cite='2']").click();
  await expect(page.getByRole("dialog").getByText("B任务原文")).toBeVisible();
  await expect(page.getByText("第2版独有原文")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "报告a", exact: true })).toHaveCount(0);
});

test("Scope 重写不会创建任务，用户确认后提交的是编辑后的 Brief", async ({ page }) => {
  await health(page);
  let submitted: typeof brief | null = null;
  await page.route("**/api/scope", route => route.fulfill({ json: { kind: "brief_pending", brief } }));
  await page.route("**/api/scope/revise", route => route.fulfill({
    json: { brief: { ...brief, brief_text: "模型修订后的范围" } },
  }));
  await page.route("**/api/jobs", route => {
    submitted = route.request().postDataJSON().brief;
    // Keep the page in place; this test owns submission, not the monitor's API responses.
    return route.fulfill({ status: 503, json: { error_code: "service_unavailable", message: "test" } });
  });
  await page.goto("/");
  await page.getByRole("textbox", { name: "研究问题" }).fill("年度收入变化");
  await page.getByRole("button", { name: "开始展开问题" }).click();
  await expect(page.getByRole("textbox", { name: "研究范围" })).toHaveValue(brief.brief_text);
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByPlaceholder("说说要怎么改，例如：多看早期的社区讨论").fill("聚焦年度变化");
  await page.getByRole("button", { name: "重写", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "研究范围" })).toHaveValue("模型修订后的范围");
  expect(submitted).toBeNull();
  await page.getByRole("textbox", { name: "研究范围" }).fill("用户最终确认的范围");
  await page.getByRole("button", { name: /^开始/ }).click();
  await expect.poll(() => submitted).toEqual({ ...brief, brief_text: "用户最终确认的范围" });
});
