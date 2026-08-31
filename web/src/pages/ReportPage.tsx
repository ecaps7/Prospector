import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { ReportAudit } from "../api/types";
import { EvidenceDrawer } from "../components/report/EvidenceDrawer";
import { ReportBody } from "../components/report/ReportBody";
import { ReportHead } from "../components/report/ReportHead";
import { ReportPending } from "../components/report/ReportPending";
import { ReportToc, type TocItem } from "../components/report/ReportToc";
import { SourceList } from "../components/report/SourceList";
import { ErrorView, LoadingView } from "../components/ui/Status";
import { apiErrorLabel } from "../lib/labels";
import { useJobRoute } from "../state/jobRoute";
import {
  parseReportDoc,
  readAudit,
  type Heading,
} from "../state/reportDoc";
import { reportGate } from "../state/reportGate";

type Loaded = { jobId: string; markdown: string; audit: ReportAudit | null };

export function ReportPage() {
  const { jobId } = useParams();
  const { job, jobError } = useJobRoute();
  // 报告和错误都记着自己属于哪个任务：换任务时旧正文必须立刻从屏幕上消失，
  // 而不是等新任务的请求回来才被顶掉。
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failure, setFailure] = useState<{ jobId: string; message: string } | null>(null);
  const [active, setActive] = useState<string>("");
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [openCitation, setOpenCitation] = useState<number | null>(null);
  // 抽屉是滑出去的，关闭那一刻内容不能跟着清空——否则读者会看着它在滑走的路上
  // 变成一张「没有原文摘录」的空卡片。所以「开着没有」和「显示的是谁」分开记。
  const [shownCitation, setShownCitation] = useState<number | null>(null);

  // 报告文件是否存在由任务快照说了算（`report.json_ref` 就是报告接口自己查的那一列），
  // 所以这里不再靠 409 试探：门一开就取正文，取不到才是真的出了问题。
  const gate = job ? reportGate(job) : null;
  const ready = gate?.kind === "ready";
  const report = loaded && loaded.jobId === jobId ? loaded : null;
  const error = failure && failure.jobId === jobId ? failure.message : null;

  useEffect(() => {
    if (!jobId || !ready) return;
    const controller = new AbortController();
    // 正文和审计文档分开取：正文只有几十 KB，审计文档嵌着每段存档原文可以到几百 KB。
    // 正文先到就先渲染，审计文档随后补上判定、未通过的跨度和摘录。
    api
      .getReportMarkdown(jobId, controller.signal)
      .then((markdown) => {
        setLoaded({ jobId, markdown, audit: null });
        return api.getReportAudit(jobId, controller.signal);
      })
      .then((audit) => {
        setLoaded((current) =>
          current && current.jobId === jobId ? { ...current, audit } : current,
        );
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setFailure({ jobId, message: apiErrorLabel(err, "无法加载报告") });
      });
    return () => controller.abort();
  }, [jobId, ready]);

  const doc = useMemo(
    () => (report ? parseReportDoc(report.markdown) : null),
    [report],
  );
  const audit = useMemo(() => readAudit(report?.audit), [report?.audit]);

  const toc = useMemo<TocItem[]>(() => {
    if (!doc) return [];
    const items: TocItem[] = headings
      .filter((item) => item.level === 2)
      .map((item) => ({ id: item.id, title: item.title }));
    if (doc.sources.length) {
      items.push({ id: "sec-src", title: "来源", divider: true });
    }
    return items;
  }, [doc, headings]);

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

  const onHeadings = useCallback((next: Heading[]) => setHeadings(next), []);
  const openSource = useCallback((citationNumber: number) => {
    setOpenCitation(citationNumber);
    setShownCitation(citationNumber);
  }, []);

  // 角标 → 来源 → 支撑过它的每一段原文。两边按「网址 + 快照版本」对上，
  // 这正是后端编号时用的那把钥匙，所以这里不算编号，只是把它认回来。
  const drawerSource = doc?.sources.find((item) => item.number === shownCitation) ?? null;
  const evidence = useMemo(() => {
    if (!drawerSource) return [];
    return audit.claimEvidence.filter(
      (item) =>
        item.excerpt.source.source_uri === drawerSource.uri &&
        item.excerpt.source.document_version === drawerSource.version,
    );
  }, [drawerSource, audit]);

  if (!job) {
    if (jobError) return <ErrorView message={jobError} />;
    return <LoadingView>正在加载任务…</LoadingView>;
  }
  if (gate && gate.kind !== "ready") return <ReportPending job={job} kind={gate.kind} />;
  if (error) return <ErrorView message={error} />;
  if (!report || !doc) return <LoadingView>正在加载报告…</LoadingView>;

  const title = doc.title || job.question || "研究报告";

  return (
    <section className="view">
      <ReportHead title={title} />

      <div className="report-layout">
        <ReportToc items={toc} active={active} />
        <div className="report-main">
          <ReportBody
            body={doc.body}
            onHeadings={onHeadings}
            onOpenSource={openSource}
          />
          {doc.sources.length ? (
            <>
              <h2 id="sec-src">来源</h2>
              <SourceList sources={doc.sources} onOpenSource={openSource} />
            </>
          ) : null}
        </div>
      </div>

      <EvidenceDrawer
        open={openCitation !== null}
        source={drawerSource}
        evidence={evidence}
        onClose={() => setOpenCitation(null)}
      />
    </section>
  );
}
