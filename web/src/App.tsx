import { useEffect, useState } from "react";
import { Outlet, useLocation, useMatch } from "react-router-dom";
import { api } from "./api/client";
import type { JobDetail } from "./api/types";
import { JobBar } from "./components/JobBar";
import { ToastProvider } from "./components/Toast";
import { TopBar } from "./components/TopBar";
import { apiErrorLabel } from "./lib/labels";
import type { JobRouteContext } from "./state/jobRoute";
import { isGateLive, reportGate } from "./state/reportGate";

const THEME_KEY = "prospector-theme";

/** 任务还在推进时的快照间隔。够快到报告一出现就接管，又不至于压着后端刷。 */
const JOB_POLL_MS = 3000;

function readTheme(): "light" | "dark" {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === "dark" ? "dark" : "light";
}

export default function App() {
  const [theme, setTheme] = useState<"light" | "dark">(readTheme);
  const [serverOk, setServerOk] = useState<boolean | null>(null);
  // Bumped by the "重试" affordance so the health effect re-runs and pings immediately.
  const [healthNonce, setHealthNonce] = useState(0);
  const [job, setJob] = useState<JobDetail | null>(null);
  // 错误跟着任务 id 走：换了任务，上一条错误就不该再挂在新任务的名下。
  const [jobFailure, setJobFailure] = useState<{ jobId: string; message: string } | null>(null);
  const location = useLocation();
  const reportMatch = useMatch("/jobs/:jobId/report");
  const monitorMatch = useMatch("/jobs/:jobId");
  const jobId = reportMatch?.params.jobId ?? monitorMatch?.params.jobId;
  const shownJob = jobId && job?.job_id === jobId ? job : null;
  const jobError = jobId && jobFailure?.jobId === jobId ? jobFailure.message : null;
  const hasJobBar = Boolean(jobId && (shownJob || jobError));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  // The sticky bars cover the top of the viewport, so anchor jumps (report TOC) and
  // sticky offsets have to know how tall they actually are. Measuring beats hard-coding:
  // the job bar comes and goes, and its content wraps on narrow screens.
  useEffect(() => {
    const bars = [".topbar", ".jobbar"]
      .map((selector) => document.querySelector(selector))
      .filter((node): node is HTMLElement => node instanceof HTMLElement);
    const measure = () => {
      const height = bars.reduce((sum, bar) => sum + bar.offsetHeight, 0);
      document.documentElement.style.setProperty("--chrome-h", `${height}px`);
    };
    measure();
    const observer = new ResizeObserver(measure);
    for (const bar of bars) observer.observe(bar);
    return () => observer.disconnect();
  }, [hasJobBar]);

  useEffect(() => {
    let cancelled = false;
    const ping = () => {
      api
        .healthz()
        .then(() => {
          if (!cancelled) setServerOk(true);
        })
        .catch(() => {
          if (!cancelled) setServerOk(false);
        });
    };
    ping();
    const timer = window.setInterval(ping, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [healthNonce]);

  // 任务没跑完就定时再取一份快照。报告页全靠这条线自己接管：报告一落库，页面就从
  // 等待态换成正文，不需要谁去点一下。任务一进终态，轮询立刻停。
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer = 0;
    const load = async (retryOnFailure: boolean) => {
      try {
        const detail = await api.getJob(jobId);
        if (cancelled) return;
        setJob(detail);
        setJobFailure(null);
        if (isGateLive(reportGate(detail).kind)) {
          timer = window.setTimeout(() => void load(true), JOB_POLL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        setJobFailure({ jobId, message: apiErrorLabel(err, "无法加载任务") });
        // 首次就失败多半是这个任务不存在，别把它转成一条永远打不通的轮询；
        // 轮到一半才断的，接着试——上一份快照还在屏幕上，等它自己接回来。
        if (retryOnFailure) timer = window.setTimeout(() => void load(true), JOB_POLL_MS);
      }
    };
    void load(false);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [jobId, location.pathname]);

  const jobContext: JobRouteContext = { job: shownJob, jobError };

  return (
    <ToastProvider>
      <TopBar
        theme={theme}
        serverOk={serverOk}
        onRetryServer={() => setHealthNonce((n) => n + 1)}
        onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
      />
      {hasJobBar && jobId ? (
        <JobBar
          jobId={jobId}
          question={shownJob ? shownJob.question || "（未命名研究）" : jobId}
          status={shownJob?.status ?? "failed"}
          outcome={shownJob?.outcome ?? null}
          verification={shownJob?.verification_status ?? null}
        />
      ) : null}
      <Outlet context={jobContext} />
    </ToastProvider>
  );
}
