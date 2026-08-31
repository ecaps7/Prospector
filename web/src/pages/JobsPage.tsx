import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { JobListItem } from "../api/types";
import { JobItem } from "../components/jobs/JobItem";
import { useToast } from "../components/Toast";
import { Spinner } from "../components/ui/Status";
import { apiErrorLabel } from "../lib/labels";

export function JobsPage() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const { toast } = useToast();

  useEffect(() => {
    let cancelled = false;
    api
      .listJobs()
      .then((items) => {
        if (!cancelled) setJobs(items);
      })
      .catch((err) => {
        if (!cancelled) setError(apiErrorLabel(err, "无法加载任务列表"));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const deleteJob = useCallback(
    async (job: JobListItem) => {
      if (busyId) return;
      const name = job.question || "未命名研究";
      if (
        !window.confirm(
          `确定把「${name}」从任务历史里删除吗？`,
        )
      )
        return;
      setBusyId(job.job_id);
      try {
        await api.deleteJob(job.job_id);
      } catch (err) {
        // 已经不在了也算删成功：列表可能是几分钟前取的。认 error_code 而不是 404，
        // 免得把「后端没有这个接口」也当成删掉了。
        if (!(err instanceof ApiError) || err.errorCode !== "job_not_found") {
          toast(apiErrorLabel(err, "无法删除这个任务"));
          setBusyId(null);
          return;
        }
      }
      setJobs((items) => items?.filter((item) => item.job_id !== job.job_id) ?? items);
      setBusyId(null);
      toast("已从任务历史里删除");
    },
    [busyId, toast],
  );

  const stopJob = useCallback(
    async (job: JobListItem) => {
      if (busyId) return;
      const name = job.question || "未命名研究";
      if (!window.confirm(`确定终止「${name}」吗？任务取消后不能继续。`)) return;
      setBusyId(job.job_id);
      try {
        // queued 的任务当场进入 cancelled，running 的先进 cancelling，在安全边界才停。
        // 状态以后端返回的为准，别在前端猜。
        const result = await api.cancelJob(job.job_id);
        setJobs(
          (items) =>
            items?.map((item) =>
              item.job_id === job.job_id ? { ...item, status: result.status } : item,
            ) ?? items,
        );
        toast(result.status === "cancelled" ? "任务已取消" : "取消请求已记录，将在安全边界停止");
      } catch (err) {
        toast(apiErrorLabel(err, "无法终止这个任务"));
      } finally {
        setBusyId(null);
      }
    },
    [busyId, toast],
  );

  return (
    <section className="view">
      <div className="jobs-head">
        <h1>任务历史</h1>
      </div>
      {error ? (
        <p className="form-error">
          {error}{" "}
          <button
            className="btn ghost sm"
            type="button"
            onClick={() => {
              setError(null);
              setJobs(null);
              setReloadKey((key) => key + 1);
            }}
          >
            重试
          </button>
        </p>
      ) : null}
      <div className="card job-list">
        {jobs === null ? (
          <div className="jobs-empty">
            <Spinner inline /> 加载中…
          </div>
        ) : jobs.length === 0 ? (
          <div className="jobs-empty">还没有研究任务。从「发起新研究」提出一个问题即可。</div>
        ) : (
          jobs.map((job) => (
            <JobItem
              busy={busyId === job.job_id}
              job={job}
              key={job.job_id}
              onDelete={() => void deleteJob(job)}
              onStop={() => void stopJob(job)}
            />
          ))
        )}
      </div>
    </section>
  );
}
