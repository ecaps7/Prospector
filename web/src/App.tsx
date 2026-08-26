import { useEffect, useState } from "react";
import { Outlet, useLocation, useMatch } from "react-router-dom";
import { api } from "./api/client";
import { JobBar } from "./components/JobBar";
import { ToastProvider } from "./components/Toast";
import { TopBar } from "./components/TopBar";

const THEME_KEY = "prospector-theme";

function readTheme(): "light" | "dark" {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === "dark" ? "dark" : "light";
}

export default function App() {
  const [theme, setTheme] = useState<"light" | "dark">(readTheme);
  const [serverOk, setServerOk] = useState<boolean | null>(null);
  // Bumped by the "重试" affordance so the health effect re-runs and pings immediately.
  const [healthNonce, setHealthNonce] = useState(0);
  const [jobMeta, setJobMeta] = useState<{
    jobId: string;
    question: string;
    status: string;
  } | null>(null);
  const location = useLocation();
  const reportMatch = useMatch("/jobs/:jobId/report");
  const monitorMatch = useMatch("/jobs/:jobId");
  const isAskHome = location.pathname === "/";
  const jobId = reportMatch?.params.jobId ?? monitorMatch?.params.jobId;
  const shownMeta = jobId && jobMeta?.jobId === jobId ? jobMeta : null;
  const hasJobBar = Boolean(shownMeta && jobId);

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

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    api
      .getJob(jobId)
      .then((job) => {
        if (!cancelled) {
          setJobMeta({
            jobId,
            question: job.question || "（未命名研究）",
            status: job.status,
          });
        }
      })
      .catch(() => {
        if (!cancelled) setJobMeta({ jobId, question: jobId, status: "failed" });
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, location.pathname]);

  return (
    <ToastProvider>
      <TopBar
        theme={theme}
        serverOk={serverOk}
        onRetryServer={() => setHealthNonce((n) => n + 1)}
        onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
      />
      {shownMeta && jobId ? (
        <JobBar jobId={jobId} question={shownMeta.question} status={shownMeta.status} />
      ) : null}
      <Outlet />
      {isAskHome ? null : (
        <footer className="pagefoot">
          <span>Prospector · 本机单用户</span>
          {import.meta.env.DEV ? <span>{window.location.host}</span> : null}
        </footer>
      )}
    </ToastProvider>
  );
}
