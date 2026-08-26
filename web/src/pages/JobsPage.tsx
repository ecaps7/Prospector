import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { JobListItem } from "../api/types";
import { JobItem } from "../components/jobs/JobItem";
import { Spinner } from "../components/ui/Status";
import { apiErrorLabel } from "../lib/labels";

export function JobsPage() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

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
          jobs.map((job) => <JobItem job={job} key={job.job_id} />)
        )}
      </div>
    </section>
  );
}
