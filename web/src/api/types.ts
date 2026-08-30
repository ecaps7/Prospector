export type EffortLevel = "quick" | "standard" | "deep";

export type JobStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed";

export type UserConstraints = {
  time_range: string;
  regions: string[];
  comparison_targets: string[];
  source_rules: string[];
  exclusions: string[];
  deliverable_rules: string[];
};

export type ResearchBrief = {
  question: string;
  brief_text: string;
  user_constraints: UserConstraints;
  output_format: string;
  language: string;
  effort: EffortLevel;
};

export type ScopeOutcome =
  | { kind: "clarify"; clarification_question: string; brief: null }
  | { kind: "brief_pending"; clarification_question: null; brief: ResearchBrief };

export type JobCreateResponse = {
  job_id: string;
  brief_id: string;
  status: "running" | "queued";
  queue_position: number | null;
};

export type JobCancelResponse = {
  job_id: string;
  status: "cancelling" | "cancelled";
};

export type TaskStatus = "pending" | "running" | "done" | "failed" | "skipped" | "cancelled";

export type JobListItem = {
  job_id: string;
  question: string | null;
  effort: EffortLevel | null;
  status: JobStatus;
  phase: string;
  outcome: string | null;
  error_code: string | null;
  /** 交付判定：`verified` / `partial` / `failed`。`outcome` 只说"报告已交付"，
   *  判定挂在报告运行上；改版前的旧任务这里是 null，判定还在 `outcome` 里。 */
  verification_status: string | null;
  created_at: string;
  updated_at: string;
};

export type JobTaskView = {
  task_id: string;
  question: string;
  status: TaskStatus;
  stop_reason: string | null;
  budget: { max_worker_rounds?: number };
  tool_calls_used: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type UsageView = {
  component: string;
  input_tokens: number;
  output_tokens: number;
  tool_calls: number;
};

export type ReportView = {
  report_id: string;
  status: string;
  verification_status: string | null;
  markdown_ref: string | null;
  json_ref: string | null;
};

export type JobDetail = JobListItem & {
  brief_id: string | null;
  language: string | null;
  plan_version: number;
  latest_event_id: number;
  tasks: JobTaskView[];
  usage: UsageView[];
  report: ReportView | null;
};

export type ApiErrorBody = {
  error_code: string;
  message: string;
};

export type ServerEvent = {
  id: number;
  event_type: string;
  payload: Record<string, unknown>;
  task_id: string | null;
  decision_round: number | null;
  created_at: string | null;
};

/**
 * 报告的审计文档，`GET /api/jobs/{id}/report?format=json` 的返回体。
 *
 * 它不是正文——正文是 `format=md` 那份 Markdown。这份记录的是"这份报告有多少
 * 站得住"：哪些跨度找到了出处、哪些没有、通读审阅怎么说。字段跟着
 * `deterministic/citation_render.py` 里拼出的那个 dict 走。
 */

export type ReportVerdict = "verified" | "partial" | "failed";

/** 全部由流水线已有的记录数出来，不依赖任何模型对自己工作的转述。 */
export type ReportHealth = {
  blocks: number;
  checked_blocks: number;
  reasoned_blocks: number;
  failed_claims: number;
  unchecked_spans: number;
  assertions_collected: number;
  assertions_used: number;
  unused_assertion_ids: string[];
  quantities_in_checked_text: number;
  quantities_in_reasoning: number;
  spans_over_citation_cap: number;
};

export type ExcerptSource = {
  title: string | null;
  author: string | null;
  published_at: string | null;
  source_uri: string;
  document_version: number;
};

export type ReportExcerpt = {
  excerpt_id: string;
  text: string;
  source: ExcerptSource;
};

/** 一条跨度和支撑它的一段存档原文。原文直接嵌在这里，不必再去问摘录接口。 */
export type ClaimEvidence = {
  claim_id: string;
  excerpt: ReportExcerpt;
  assertion_ids: string[];
  /** 核验时对这个来源本身提出的保留意见，例如"综合类来源，证据强度较低"。 */
  source_caveats: string[];
};

/** 一处没能找到出处、或被就地降级的正文跨度。 */
export type AttributionFinding = {
  finding_id: string;
  kind: "attribution" | "in_place_downgrade";
  claim_id: string | null;
  block_id: string;
  start_offset: number | null;
  end_offset: number | null;
  text: string;
  reason: string;
};

/** 通读全文之后对整篇提出的问题，落到段落而不是跨度上。 */
export type ReviewFinding = {
  kind: "brief_response" | "user_constraint" | "material_omission" | "conclusion_integrity";
  reason: string;
  block_ids: string[];
};

export type ReadthroughFinding = {
  kind: "dangling_reference" | "broken_transition" | "summary_mismatch" | "orphaned_passage";
  block_ids: string[];
  reason: string;
};

export type ReportAudit = {
  verification_status: ReportVerdict;
  inline_citation_cap: number;
  health: ReportHealth;
  readthrough: { findings: ReadthroughFinding[] } | null;
  claim_evidence: ClaimEvidence[];
  blocking_findings: AttributionFinding[];
  whole_report_review: {
    blocking_findings: ReviewFinding[];
    key_block_ids: string[];
  };
};
