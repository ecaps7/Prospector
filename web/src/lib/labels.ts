/**
 * The single place that turns backend enum values into words a user reads.
 * Nothing outside this file should render a raw `status`, `outcome`, `phase`,
 * `effort`, `research_stage` or `error_code` straight from the API.
 *
 * Unknown values fall through to the raw string on purpose: silently swallowing
 * a value the backend added is worse than showing it once and fixing the map.
 */

import { ApiError } from "../api/client";

const EFFORT: Record<string, string> = {
  quick: "快速",
  standard: "标准",
  deep: "深入",
};

const LANGUAGE: Record<string, string> = {
  zh: "中文",
  en: "English",
};

const OUTCOME: Record<string, string> = {
  verified: "已逐句核对",
  partial: "部分核对",
  draft_rendered: "未逐句核对",
};

const STATUS: Record<string, string> = {
  initialize: "准备中",
  queued: "排队中",
  running: "研究中",
  completed: "已完成",
  failed: "失败",
  cancelling: "正在取消",
  cancelled: "已取消",
};

const PHASE: Record<string, string> = {
  initialize: "准备中",
  queued: "排队中",
  running: "进行中",
  research: "搜集资料",
  verifier: "核验证据",
  composition_pending: "准备撰写",
  writing: "撰写初稿",
  verifying: "逐句核对",
  revising: "修订初稿",
  verified: "核对完成",
  revisions_exhausted: "修订次数用尽",
  draft_rendered: "报告已生成",
  cancelling: "正在取消",
  cancelled: "已取消",
};

const STAGE: Record<string, string> = {
  scout: "摸底",
  deep_dive: "深挖",
  verify: "求证",
};

const MODE: Record<string, string> = {
  factual: "事实核验",
  comparison: "对比",
  counterargument: "反证",
  risk_scan: "风险扫描",
  timeline: "时间线",
};

const ERROR: Record<string, string> = {
  research_budget_exhausted_without_evidence: "调查预算用尽，仍未找到足够证据",
  verifier_major_gap: "证据核对发现重大缺口",
  verifier_output_invalid: "证据核对环节返回了无法解析的结果",
  report_verifier_contract_error: "报告核对环节返回了无法解析的结果",
  writer_contract_error: "撰写环节返回了无法解析的结果",
  planner_schema_error_limit: "任务规划连续返回无法解析的结果",
  draft_render_error: "报告生成失败",
  job_execution_error: "任务执行出错",
  service_unavailable: "模型服务暂时不可用",
  validation_error: "请求内容未通过校验",
  report_not_ready: "报告尚未就绪",
  job_not_found: "找不到这个任务",
  job_not_cancellable: "这个任务已经停了，无法再取消",
  llm_not_configured: "模型服务还没配置好，请检查 .env 里的模型密钥",
  not_found: "找不到这个地址",
  excerpt_not_found: "找不到这段摘录",
  invalid_last_event_id: "事件续传位置无效",
};

const pick = (map: Record<string, string>, value: string | null | undefined): string =>
  value ? (map[value] ?? value) : "";

export const effortLabel = (value: string | null | undefined): string => pick(EFFORT, value);
export const languageLabel = (value: string | null | undefined): string => pick(LANGUAGE, value);
export const phaseLabel = (value: string | null | undefined): string => pick(PHASE, value);
export const stageLabel = (value: string | null | undefined): string => pick(STAGE, value);
export const modeLabel = (value: string | null | undefined): string => pick(MODE, value);
export const errorLabel = (value: string | null | undefined): string => pick(ERROR, value);

/**
 * The one line to show when an API call fails. The backend's `message` field is
 * English prose aimed at developers, so it never reaches the screen — we look up
 * the stable `error_code` instead, and let the caller word the fallback for
 * codes we haven't mapped (and for failures that never reached the backend).
 */
export function apiErrorLabel(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  return ERROR[error.errorCode] ?? fallback;
}

/** What a finished job produced, e.g. "部分核对". Empty when still running. */
export const outcomeLabel = (value: string | null | undefined): string => pick(OUTCOME, value);

/** One phrase covering both the job's status and, once done, how it ended. */
export function jobStatusLabel(status: string, outcome?: string | null): string {
  if (status === "completed") {
    if (outcome === "partial") return "部分完成";
    if (outcome === "draft_rendered") return "已完成 · 未逐句核对";
    return "已完成";
  }
  return pick(STATUS, status);
}

/** The seven steps of the pipeline, named for the person watching them. */
export const PHASE_STEPS = [
  "确认问题",
  "制定计划",
  "搜集资料",
  "核验证据",
  "撰写初稿",
  "逐句核对",
  "输出报告",
] as const;
