import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { ExcerptView, ReportJson, ReportSource } from "../api/types";
import { EvidenceDrawer } from "../components/report/EvidenceDrawer";
import { ReportHead } from "../components/report/ReportHead";
import { ReportToc, type TocItem } from "../components/report/ReportToc";
import { SourceList } from "../components/report/SourceList";
import { StatementParagraphs } from "../components/report/StatementParagraphs";
import { ErrorView, LoadingView } from "../components/ui/Status";
import { apiErrorLabel } from "../lib/labels";

function slug(index: number, title: string): string {
  return `sec-${index}-${title.slice(0, 12)}`;
}

export function ReportPage() {
  const { jobId } = useParams();
  const [report, setReport] = useState<ReportJson | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
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
          setError(apiErrorLabel(err, "无法加载报告"));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, reloadKey]);

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
      // A heading counts as "the one you're reading" once it passes under the sticky
      // bars — the same line the TOC scrolls headings to, so highlight and jump agree.
      const chrome =
        parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--chrome-h")) || 52;
      const readingLine = chrome + 24;
      let current = toc[0]?.id ?? "";
      for (const item of toc) {
        const el = document.getElementById(item.id);
        if (el && el.getBoundingClientRect().top < readingLine) current = item.id;
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
      const excerpts = source.excerpt_ids.length ? await api.listExcerpts(jobId, source.excerpt_ids) : [];
      setDrawer({ source, excerpts });
    } catch (err) {
      setExcerptError(apiErrorLabel(err, "无法读取摘录"));
    } finally {
      setLoadingExcerpt(false);
    }
  };

  if (error)
    return (
      <ErrorView
        message={error}
        tone="muted"
        onRetry={() => {
          setError(null);
          setReloadKey((key) => key + 1);
        }}
      />
    );
  if (!report) return <LoadingView>正在加载报告…</LoadingView>;

  const failed = new Set(report.failed_statement_ids ?? []);
  const citations = report.statement_citations ?? {};
  const sourceByNumber = new Map(report.sources.map((source) => [source.citation_number, source]));
  const verified = report.verification_status === "verified";
  const statementCount = [
    ...report.draft.introduction,
    ...report.draft.sections.flatMap((section) => section.paragraphs),
    ...report.draft.conclusion,
  ].reduce((sum, paragraph) => sum + paragraph.statements.length, 0);

  const bodyProps = { citations, failed, sourceByNumber, onOpenSource: (s: ReportSource) => void openSource(s) };

  return (
    <section className="view">
      <ReportHead
        title={report.draft.title}
        verified={verified}
        statementCount={statementCount}
        failedCount={failed.size}
      />

      <div className="report-layout">
        <ReportToc items={toc} active={active} />
        <article className="report-body">
          <h2 id="sec-intro">引言</h2>
          <StatementParagraphs paragraphs={report.draft.introduction} {...bodyProps} />
          {report.draft.sections.map((section, index) => (
            <section key={section.section_id}>
              <h2 id={slug(index + 1, section.title)}>{section.title}</h2>
              <StatementParagraphs paragraphs={section.paragraphs} {...bodyProps} />
            </section>
          ))}
          <h2 id="sec-conclusion">综合结论</h2>
          <StatementParagraphs paragraphs={report.draft.conclusion} {...bodyProps} />
          <h2 id="sec-src">来源</h2>
          <SourceList sources={report.sources} onOpenSource={bodyProps.onOpenSource} />
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
