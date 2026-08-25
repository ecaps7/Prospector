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

export type JobListItem = {
  job_id: string;
  question: string | null;
  effort: EffortLevel | null;
  status: JobStatus;
  phase: string;
  outcome: string | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
};

export type JobTaskView = {
  task_id: string;
  question: string;
  subjects: string[];
  research_stage: string;
  research_mode: string;
  status: string;
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

export type ExcerptView = {
  excerpt_id: string;
  text: string;
  source_uri: string;
  document_version: number;
  title: string | null;
  author: string | null;
  published_at: string | null;
  locator: Record<string, unknown>;
};

export type StatementKind = "evidence" | "derived" | "elaboration" | "limitation";

export type ReportStatement = {
  statement_id: string;
  text: string;
  kind: StatementKind;
  candidate_excerpt_ids: string[];
  premise_statement_ids: string[];
};

export type ReportParagraph = {
  paragraph_id: string;
  statements: ReportStatement[];
};

export type ReportSection = {
  section_id: string;
  title: string;
  paragraphs: ReportParagraph[];
};

export type ReportDraft = {
  title: string;
  introduction: ReportParagraph[];
  sections: ReportSection[];
  conclusion: ReportParagraph[];
};

export type ReportSource = {
  citation_number: number;
  source_uri: string;
  document_version: number;
  title: string | null;
  author: string | null;
  published_at: string | null;
  excerpt_ids: string[];
};

export type ReportJson = {
  verification_status: "pending" | "verified" | "partial";
  failed_statement_ids: string[];
  job_id: string;
  draft: ReportDraft;
  statement_citations: Record<string, number[]>;
  citation_excerpt_ids?: Record<string, string[]>;
  sources: ReportSource[];
};
