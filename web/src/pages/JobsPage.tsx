import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { JobListItem } from "../api/types";
import { JobItem } from "../components/jobs/JobItem";
import { Chip } from "../components/ui/Tag";
import { Spinner } from "../components/ui/Status";

export function JobsPage() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listJobs()
      .then((items) => {
        if (!cancelled) setJobs(items);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "无法加载任务列表");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="view">
      <div className="jobs-head">
        <h1>任务历史</h1>
        <Chip>本机单用户 · 一次只运行一个任务</Chip>
      </div>
      {error ? <p className="form-error">{error}</p> : null}
      <div className="card job-list">
        {jobs === null ? (
          <div className="jobs-empty">
            <Spinner inline /> 加载中…
          </div>
        ) : jobs.length === 0 ? (
          <div className="jobs-empty">还没有研究任务。从「发起新研究」提出一个问题即可。</div>
        ) : (
          jobs.map((job) => <JobItem job={job} key={job.job_id} />)
        )}
      </div>
    </section>
  );
}
