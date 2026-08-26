import type {
  ExcerptView,
  JobCancelResponse,
  JobCreateResponse,
  JobDetail,
  JobListItem,
  ReportJson,
  ResearchBrief,
  ScopeOutcome,
} from "./types";

export class ApiError extends Error {
  status: number;
  errorCode: string;

  constructor(status: number, errorCode: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.errorCode = errorCode;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let errorCode = "service_unavailable";
  let message = `Prospector 返回 HTTP ${response.status}`;
  try {
    const body = (await response.json()) as { error_code?: string; message?: string };
    if (body.error_code) errorCode = body.error_code;
    if (body.message) message = body.message;
  } catch {
    /* keep defaults */
  }
  return new ApiError(response.status, errorCode, message);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function emptyConstraints(): ResearchBrief["user_constraints"] {
  return {
    time_range: "",
    regions: [],
    comparison_targets: [],
    source_rules: [],
    exclusions: [],
    deliverable_rules: [],
  };
}

export const api = {
  healthz: () => request<{ status: "ok" }>("/api/healthz"),

  // Scope blocks on an LLM call for tens of seconds, so both entry points take a
  // signal: without one the caller has no way to stop waiting.
  scope: (
    payload: {
      question: string;
      effort: ResearchBrief["effort"];
      language: string;
      clarification_question?: string;
      clarification_answer?: string;
    },
    signal?: AbortSignal,
  ) => request<ScopeOutcome>("/api/scope", { method: "POST", body: JSON.stringify(payload), signal }),

  reviseScope: (
    payload: {
      question: string;
      previous_brief: ResearchBrief;
      revision_note: string;
      effort: ResearchBrief["effort"];
      language: string;
    },
    signal?: AbortSignal,
  ) =>
    request<{ brief: ResearchBrief }>("/api/scope/revise", {
      method: "POST",
      body: JSON.stringify(payload),
      signal,
    }),

  createJob: (brief: ResearchBrief) =>
    request<JobCreateResponse>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ brief }),
    }),

  listJobs: () => request<JobListItem[]>("/api/jobs"),

  getJob: (jobId: string) => request<JobDetail>(`/api/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    request<JobCancelResponse>(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ requested_via: "web_monitor" }),
    }),

  getReportJson: (jobId: string) => request<ReportJson>(`/api/jobs/${jobId}/report?format=json`),

  listExcerpts: (jobId: string, ids: string[]) => {
    const params = new URLSearchParams();
    for (const id of ids) params.append("ids", id);
    return request<ExcerptView[]>(`/api/jobs/${jobId}/excerpts?${params}`);
  },
};
