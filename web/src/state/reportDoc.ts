import { Marked, type Tokens } from "marked";

import type {
  AttributionFinding,
  ClaimEvidence,
  ReadthroughFinding,
  ReportAudit,
  ReportHealth,
  ReportVerdict,
  ReviewFinding,
} from "../api/types";

/**
 * 后端交付的报告是一份 Markdown 文档，不是结构化草稿。它由三段拼成，
 * 由 `deterministic/citation_render.py` 确定性地生成：
 *
 *   1. 开头一段引用块，写核对情况；
 *   2. 正文，脚注角标 `[^n]` 已经插在该插的位置上；
 *   3. `## 来源`，下面是 `[^n]: 标题，网址（版本 v）` 的脚注定义。
 *
 * 前端只负责把它显示出来。编号是后端算的，这里从不重算——只把角标和定义
 * 按同一个编号对上，好让读者点得动。
 */

export type ReportSource = {
  number: number;
  label: string;
  uri: string;
  version: number;
};

export type ReportDoc = {
  /** 开头的核对情况引用块，原样的 Markdown；没有就是空串。 */
  health: string;
  /** 报告标题，也就是开头那个一级标题的文字；没有就是空串。 */
  title: string;
  /** 正文 Markdown，不含核对情况、标题和末尾的来源。 */
  body: string;
  sources: ReportSource[];
};

const SOURCES_HEADING = /^##\s+来源\s*$/m;
// `[^3]: 标题，https://…（版本 1）`。标题里出现中文逗号是常事，所以从右边找分隔符。
const FOOTNOTE_DEF = /^\[\^(\d+)\]:\s*(.+)$/;

function parseSource(line: string): ReportSource | null {
  const match = FOOTNOTE_DEF.exec(line.trim());
  if (!match) return null;
  const [, rawNumber, rest] = match;
  const version = /（版本\s*(\d+)）\s*$/.exec(rest);
  const withoutVersion = version ? rest.slice(0, version.index) : rest;
  const split = withoutVersion.lastIndexOf("，");
  const label = split > 0 ? withoutVersion.slice(0, split).trim() : withoutVersion.trim();
  const uri = split > 0 ? withoutVersion.slice(split + 1).trim() : "";
  return {
    number: Number(rawNumber),
    label: label || uri,
    uri,
    version: version ? Number(version[1]) : 1,
  };
}

/** 把交付的 Markdown 拆成核对情况、正文和来源三段。 */
export function parseReportDoc(markdown: string): ReportDoc {
  const text = markdown.replace(/\r\n/g, "\n").trim();
  const heading = SOURCES_HEADING.exec(text);
  const beforeSources = heading ? text.slice(0, heading.index) : text;
  const sourceLines = heading ? text.slice(heading.index + heading[0].length) : "";
  const sources = sourceLines
    .split("\n")
    .map(parseSource)
    .filter((item): item is ReportSource => item !== null)
    .sort((left, right) => left.number - right.number);

  const lines = beforeSources.trim().split("\n");
  const health: string[] = [];
  // 核对情况是文档开头连续的引用行，正文里的引用块不会顶在第一行。
  while (lines.length && (lines[0].startsWith(">") || (health.length > 0 && !lines[0].trim()))) {
    const line = lines.shift() as string;
    if (!line.trim() && !lines[0]?.startsWith(">")) break;
    health.push(line);
  }
  // 标题单独拎出来：页面把它和判定摆在一起，正文里再渲染一遍就成了重复的大字。
  const rest = lines.join("\n").trim().split("\n");
  const titleLine = /^#\s+(.+?)\s*$/.exec(rest[0] ?? "");
  const title = titleLine ? titleLine[1] : "";
  if (titleLine) rest.shift();
  return {
    health: health.join("\n").trim(),
    title,
    body: rest.join("\n").trim(),
    sources,
  };
}

export type Heading = { id: string; title: string; level: number };

export type RenderedBody = {
  html: string;
  headings: Heading[];
};

const SAFE_PROTOCOL = /^(https?:|mailto:)/i;

function slugify(text: string, index: number): string {
  const base = text
    .trim()
    .replace(/\s+/g, "-")
    .replace(/[^\p{L}\p{N}-]/gu, "")
    .slice(0, 32);
  return `sec-${index}-${base}`;
}

/**
 * 正文的 Markdown → HTML。
 *
 * 报告的字句来自抓回来的网页，所以这里当不可信文本处理：原始 HTML 一律丢掉
 * （Writer 的提示词里也禁止它），链接只放行 http/https/mailto。marked 默认会转义
 * 文本，剩下的口子就是这两处。
 */
export function renderReportBody(body: string): RenderedBody {
  const headings: Heading[] = [];
  const marked = new Marked({ gfm: true, breaks: false });

  marked.use({
    extensions: [
      {
        name: "citationMark",
        level: "inline",
        start: (src: string) => src.indexOf("[^"),
        tokenizer(src: string) {
          const match = /^\[\^(\d+)\]/.exec(src);
          if (!match) return undefined;
          return { type: "citationMark", raw: match[0], number: Number(match[1]) };
        },
        renderer(token) {
          const number = (token as unknown as { number: number }).number;
          // 角标本身是按钮：读者点它就该看到这句话背后的原文。
          return `<button type="button" class="cite" data-cite="${number}">${number}</button>`;
        },
      },
    ],
    renderer: {
      html: () => "",
      heading({ tokens, depth }: Tokens.Heading) {
        const title = this.parser.parseInline(tokens);
        const plain = title.replace(/<[^>]*>/g, "").trim();
        const id = slugify(plain, headings.length + 1);
        headings.push({ id, title: plain, level: depth });
        return `<h${depth} id="${id}">${title}</h${depth}>\n`;
      },
      link({ href, title, tokens }: Tokens.Link) {
        const text = this.parser.parseInline(tokens);
        if (!SAFE_PROTOCOL.test(href)) return text;
        const attr = title ? ` title="${title.replace(/"/g, "&quot;")}"` : "";
        return `<a href="${href}"${attr} target="_blank" rel="noreferrer noopener">${text}</a>`;
      },
    },
  });

  return { html: marked.parse(body) as string, headings };
}

/**
 * 审计文档里这一页真正用到的东西。
 *
 * 改版前交付的 `report.json` 是另一份完全不同的文档（`draft` / `failed_statement_ids`
 * / `statement_citations`），那些任务的报告现在还能下载，也还会有人翻。这里把两种
 * 文档都收成同一个形状：老文档给不出核对情况和未通过的跨度，那就是没有，正文照常显示。
 */
export type AuditView = {
  verdict: ReportVerdict | null;
  health: ReportHealth | null;
  spans: AttributionFinding[];
  review: ReviewFinding[];
  readthrough: ReadthroughFinding[];
  claimEvidence: ClaimEvidence[];
};

const EMPTY_AUDIT: AuditView = {
  verdict: null,
  health: null,
  spans: [],
  review: [],
  readthrough: [],
  claimEvidence: [],
};

const VERDICTS = new Set(["verified", "partial", "failed"]);

export function readAudit(raw: Partial<ReportAudit> | null | undefined): AuditView {
  if (!raw) return EMPTY_AUDIT;
  const verdict = raw.verification_status;
  return {
    verdict: verdict && VERDICTS.has(verdict) ? verdict : null,
    health: raw.health ?? null,
    spans: raw.blocking_findings ?? [],
    review: raw.whole_report_review?.blocking_findings ?? [],
    readthrough: raw.readthrough?.findings ?? [],
    claimEvidence: raw.claim_evidence ?? [],
  };
}

export function findingCount(audit: AuditView): number {
  return audit.spans.length + audit.review.length + audit.readthrough.length;
}
