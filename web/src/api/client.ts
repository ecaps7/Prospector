import type {
  JobCancelResponse,
  JobCreateResponse,
  JobDetail,
  JobListItem,
  ReportAudit,
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

  /**
   * 把任务从「任务历史」里删掉。后端只是不再列出它：证据、快照、报告对象都还在，
   * 因为一份网页快照是跨任务共享的。没停下来的任务删不掉，得先取消。
   */
  deleteJob: (jobId: string) => request<void>(`/api/jobs/${jobId}`, { method: "DELETE" }),

  cancelJob: (jobId: string) =>
    request<JobCancelResponse>(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ requested_via: "web_monitor" }),
    }),

  /**
   * 报告正文。角标和来源编号都是后端确定性渲染好的，前端只显示。
   * 这里要的是 text 不是 JSON，所以不走 `request`。
   */
  getReportMarkdown: async (jobId: string, signal?: AbortSignal): Promise<string> => {
    const response = await fetch(`/api/jobs/${jobId}/report?format=md`, {
      headers: { Accept: "text/markdown" },
      signal,
    });
    if (!response.ok) throw await parseError(response);
    return response.text();
  },

  /** 报告的审计文档：核对情况、未通过的跨度、每条出处的存档原文。 */
  getReportAudit: (jobId: string, signal?: AbortSignal) =>
    request<ReportAudit>(`/api/jobs/${jobId}/report?format=json`, { signal }),

  // 摘录接口还在（CLI 用它按 id 取原文），但报告页不再需要：审计文档里每条出处
  // 都嵌着那段存档原文，再跑一趟只是多一次往返。
};
