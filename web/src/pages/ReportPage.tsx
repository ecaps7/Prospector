import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { ExcerptView, ReportJson, ReportParagraph, ReportSource, StatementKind } from "../api/types";
import { EvidenceDrawer } from "../components/EvidenceDrawer";

const KIND_LABEL: Record<StatementKind, string> = {
  evidence: "引证句",
  derived: "推理句",
  elaboration: "承转句",
  limitation: "局限句",
};

type TocItem = { id: string; title: string; divider?: boolean };

function slug(index: number, title: string): string {
  return `sec-${index}-${title.slice(0, 12)}`;
}

export function ReportPage() {
  const { jobId } = useParams();
  const [report, setReport] = useState<ReportJson | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<string>("");
  const [drawer, setDrawer] = useState<{ source: ReportSource; excerpts: ExcerptView[] } | null>(null);
  const [loadingExcerpt, setLoadingExcerpt] = useState(false);
  const [excerptError, setExcerptError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    api
      .getReportJson(jobId)
      .then((payload) => {
        if (!cancelled) setReport(payload);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.errorCode === "report_not_ready") {
          setError("报告尚未就绪。研究完成后可在此阅读。");
        } else {
          setError(err instanceof ApiError ? err.message : "无法加载报告");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const toc = useMemo<TocItem[]>(() => {
    if (!report) return [];
    const items: TocItem[] = [{ id: "sec-intro", title: "引言" }];
    report.draft.sections.forEach((section, index) => {
      items.push({ id: slug(index + 1, section.title), title: section.title });
    });
    items.push({ id: "sec-conclusion", title: "综合结论" });
    items.push({ id: "sec-src", title: "来源", divider: true });
    return items;
  }, [report]);

  useEffect(() => {
    const onScroll = () => {
      let current = toc[0]?.id ?? "";
      for (const item of toc) {
        const el = document.getElementById(item.id);
        if (el && el.getBoundingClientRect().top < 120) current = item.id;
      }
      setActive(current);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, [toc]);

  const openSource = async (source: ReportSource) => {
    if (!jobId) return;
    setDrawer({ source, excerpts: [] });
    setExcerptError(null);
    setLoadingExcerpt(true);
    try {
      const excerpts = source.excerpt_ids.length
        ? await api.listExcerpts(jobId, source.excerpt_ids)
        : [];
      setDrawer({ source, excerpts });
    } catch (err) {
      setExcerptError(err instanceof ApiError ? err.message : "无法读取摘录");
    } finally {
      setLoadingExcerpt(false);
    }
  };

  if (error) {
    return (
      <section className="view">
        <p className="muted">{error}</p>
      </section>
    );
  }
  if (!report) {
    return (
      <section className="view">
        <div className="scope-status">
          <span className="spinner" />
          <span>正在加载报告…</span>
        </div>
      </section>
    );
  }

  const failed = new Set(report.failed_statement_ids ?? []);
  const citations = report.statement_citations ?? {};
  const sourceByNumber = new Map(report.sources.map((source) => [source.citation_number, source]));
  const verified = report.verification_status === "verified";
  const statementCount = [
    ...report.draft.introduction,
    ...report.draft.sections.flatMap((section) => section.paragraphs),
    ...report.draft.conclusion,
  ].reduce((sum, paragraph) => sum + paragraph.statements.length, 0);

  const renderParagraphs = (paragraphs: ReportParagraph[]) =>
    paragraphs.map((paragraph) => (
      <p key={paragraph.paragraph_id}>
        {paragraph.statements.map((statement) => {
          const numbers = citations[statement.statement_id] ?? [];
          const isFailed = failed.has(statement.statement_id);
          return (
            <span key={statement.statement_id} className={isFailed ? "stmt-failed" : undefined}>
              {statement.kind === "limitation" ? <strong>信息局限：</strong> : null}
              {statement.text}
              {isFailed ? (
                <span className="fail-mark">未通过核对 · {KIND_LABEL[statement.kind]}</span>
              ) : (
                numbers.map((number) => {
                  const source = sourceByNumber.get(number);
                  return (
                    <button
                      key={`${statement.statement_id}-${number}`}
                      className="cite"
                      type="button"
                      onClick={() => source && void openSource(source)}
                    >
                      {number}
                    </button>
                  );
                })
              )}
            </span>
          );
        })}
      </p>
    ));

  return (
    <section className="view">
      <div className="report-head">
        <div className="report-title-row">
          <h1>{report.draft.title}</h1>
          <span className={`vbadge ${verified ? "ok" : "warn"}`}>
            {verified
              ? `✓ 已逐句核对 · ${statementCount} 句全部通过`
              : `⚠ 部分核对 · ${failed.size} 句未通过（partial）`}
          </span>
        </div>
        {!verified ? (
          <div className="report-note">
            本报告标记为 {report.verification_status}：未通过核对的句子保留原文、不带引用角标并如实标出——与其硬凑一个干净结论，不如如实标出哪些句子没通过核对。
          </div>
        ) : null}
      </div>

      <div className="report-layout">
        <aside className="toc">
          <div className="panel-title">目录</div>
          {toc.map((item) => (
            <span key={item.id}>
              {item.divider ? <div className="toc-div" /> : null}
              <a
                href={`#${item.id}`}
                className={active === item.id ? "on" : ""}
                onClick={(event) => {
                  event.preventDefault();
                  document.getElementById(item.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
              >
                {item.title}
              </a>
            </span>
          ))}
        </aside>
        <article className="report-body">
          <h2 id="sec-intro">引言</h2>
          {renderParagraphs(report.draft.introduction)}
          {report.draft.sections.map((section, index) => (
            <section key={section.section_id}>
              <h2 id={slug(index + 1, section.title)}>{section.title}</h2>
              {renderParagraphs(section.paragraphs)}
            </section>
          ))}
          <h2 id="sec-conclusion">综合结论</h2>
          {renderParagraphs(report.draft.conclusion)}
          <h2 id="sec-src">来源</h2>
          <div className="sources-list">
            {report.sources.map((source) => (
              <button
                key={source.citation_number}
                className="source-item"
                type="button"
                onClick={() => void openSource(source)}
              >
                <span className="source-num">{source.citation_number}</span>
                <div className="source-main">
                  <div className="source-title">
                    {source.title || source.source_uri}
                    {source.author ? ` · ${source.author}` : ""}
                  </div>
                  <span className="source-uri">{source.source_uri}</span>
                </div>
              </button>
            ))}
          </div>
        </article>
      </div>

      <EvidenceDrawer
        open={drawer !== null}
        source={drawer?.source ?? null}
        excerpts={drawer?.excerpts ?? []}
        loading={loadingExcerpt}
        error={excerptError}
        onClose={() => setDrawer(null)}
      />
    </section>
  );
}
