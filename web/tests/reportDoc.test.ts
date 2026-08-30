import assert from "node:assert/strict";
import test from "node:test";

import { findingCount, parseReportDoc, readAudit, renderReportBody } from "../src/state/reportDoc.ts";

const DOC = [
  "> **核对情况（部分核对未通过）**：全文 36 段，其中 27 段含有已核对的具体事实。",
  "",
  "# 1929年金融危机",
  "",
  "## 一、繁荣如何制造脆弱性",
  "",
  "1920年代美国并非直线繁荣[^1]。EH.net 指出，1923年后恢复较平稳增长[^1][^2]。",
  "",
  "## 来源",
  "",
  "[^1]: The U.S. Economy in the 1920s – EH.net，https://eh.net/encyclopedia/the-u-s-economy-in-the-1920s/（版本 1）",
  "[^2]: 信贷繁荣，1920年代，https://example.org/a（版本 3）",
].join("\n");

test("交付的文档拆成核对情况、标题、正文、来源", () => {
  const doc = parseReportDoc(DOC);
  assert.match(doc.health, /^> \*\*核对情况/);
  assert.equal(doc.title, "1929年金融危机");
  // 标题在抬头上和判定摆在一起，正文里不能再出现一遍。
  assert.ok(doc.body.startsWith("## 一、繁荣如何制造脆弱性"));
  assert.ok(!doc.body.includes("## 来源"));
  assert.ok(!doc.body.includes("核对情况"));
  assert.equal(doc.sources.length, 2);
});

test("来源行拆得出编号、标题、网址和版本，标题里的逗号不会把网址切坏", () => {
  const [first, second] = parseReportDoc(DOC).sources;
  assert.deepEqual(first, {
    number: 1,
    label: "The U.S. Economy in the 1920s – EH.net",
    uri: "https://eh.net/encyclopedia/the-u-s-economy-in-the-1920s/",
    version: 1,
  });
  // 标题自己带中文逗号，从右边找分隔符才不会把「信贷繁荣」当成整条标题。
  assert.equal(second.label, "信贷繁荣，1920年代");
  assert.equal(second.uri, "https://example.org/a");
  assert.equal(second.version, 3);
});

test("角标渲染成可点的按钮，编号照抄后端", () => {
  const { html } = renderReportBody(parseReportDoc(DOC).body);
  assert.match(html, /<button type="button" class="cite" data-cite="1">1<\/button>/);
  assert.match(html, /data-cite="2"/);
});

test("标题拿到锚点，目录和正文用的是同一套 id", () => {
  const { html, headings } = renderReportBody(parseReportDoc(DOC).body);
  const section = headings.find((item) => item.level === 2);
  assert.ok(section, "应当认出二级标题");
  assert.equal(section.title, "一、繁荣如何制造脆弱性");
  assert.ok(html.includes(`id="${section.id}"`));
});

test("原始 HTML 一律丢掉，正文里的字句来自抓回来的网页", () => {
  const { html } = renderReportBody('正文<img src=x onerror="alert(1)">与<script>alert(2)</script>结尾');
  assert.ok(!html.includes("<img"), html);
  assert.ok(!html.includes("<script"), html);
  assert.ok(!html.includes("onerror"), html);
});

test("只有 http/https/mailto 的链接会渲染成链接", () => {
  const good = renderReportBody("[来源](https://example.org/a)").html;
  assert.match(good, /<a href="https:\/\/example\.org\/a"[^>]*rel="noreferrer noopener"/);
  const bad = renderReportBody("[点我](javascript:alert(1))").html;
  assert.ok(!bad.includes("<a "), bad);
  assert.ok(bad.includes("点我"), bad);
});

test("代码块里的 [^1] 是字面量，不该变成角标", () => {
  const { html } = renderReportBody("```\n[^1]\n```");
  assert.ok(!html.includes("data-cite"), html);
});

test("没有来源段的文档也读得下来", () => {
  const doc = parseReportDoc("# 标题\n\n正文一句话。");
  assert.equal(doc.health, "");
  assert.equal(doc.sources.length, 0);
  assert.equal(doc.title, "标题");
  assert.equal(doc.body, "正文一句话。");
});

test("改版前的审计文档读不出核对情况，但不该让报告页崩掉", () => {
  // 那时的 report.json 是另一份文档：draft / failed_statement_ids / statement_citations，
  // 没有 health，也没有 whole_report_review。这些任务的报告现在还能下载，还会有人翻。
  const legacy = {
    verification_status: "partial",
    draft: { title: "旧报告" },
    failed_statement_ids: ["s1"],
    statement_citations: {},
    sources: [],
  } as unknown as Parameters<typeof readAudit>[0];
  const audit = readAudit(legacy);
  assert.equal(audit.verdict, "partial");
  assert.equal(audit.health, null);
  assert.deepEqual(audit.spans, []);
  assert.deepEqual(audit.review, []);
  assert.deepEqual(audit.claimEvidence, []);
  assert.equal(findingCount(audit), 0);
});

test("审计文档还没到（或取不到）时也读得出一个空的形状", () => {
  assert.equal(findingCount(readAudit(null)), 0);
  assert.equal(readAudit(undefined).verdict, null);
});

test("当前的审计文档三种问题分别归位", () => {
  const audit = readAudit({
    verification_status: "failed",
    health: { blocks: 4 },
    blocking_findings: [{ finding_id: "f1" }],
    whole_report_review: { blocking_findings: [{ kind: "material_omission" }], key_block_ids: [] },
    readthrough: { findings: [{ kind: "broken_transition" }] },
    claim_evidence: [{ claim_id: "c1" }],
  } as unknown as Parameters<typeof readAudit>[0]);
  assert.equal(audit.verdict, "failed");
  assert.equal(audit.health?.blocks, 4);
  assert.equal(findingCount(audit), 3);
  assert.equal(audit.claimEvidence.length, 1);
});
