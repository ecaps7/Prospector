/**
 * The single place that turns backend enum values into words a user reads.
 * Nothing outside this file should render a raw `status`, `outcome`, `phase`,
 * `effort` or `error_code` straight from the API.
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

/**
 * 任务的收尾方式。改版后 `report_rendered` 是唯一的成功收尾——"报告已交付"，
 * 交付的成色由 VERIFICATION 单独说。verified / partial / draft_rendered 是改版前
 * 的写法，旧任务的快照里还留着，所以留在表里。
 */
const OUTCOME: Record<string, string> = {
  report_rendered: "报告已交付",
  // 中途态：证据核验放行后、报告交付前，任务的 outcome 停在这里。
  ready_for_writer: "证据已放行，待成文",
  verified: "已逐句核对",
  partial: "部分核对",
  draft_rendered: "未逐句核对",
  cancelled: "已取消",
  failed: "失败",
};

/**
 * 报告的交付判定。后端在通读审阅之后定这个词：出处和审阅都没有拦截项是
 * `verified`；只有边角处没站住是 `partial`；主结论没站住是 `failed`——注意
 * `failed` 说的是报告没通过核验，不是任务跑挂了，报告照样交付。
 */
const VERIFICATION: Record<string, string> = {
  verified: "已核验",
  partial: "部分核验",
  failed: "未通过核验",
  pending: "待核验",
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

/**
 * 后端的阶段词。监控页会重放历史事件，所以改版前才会出现的阶段
 * （verifying / revisions_exhausted / draft_rendered）留着不删，
 * 否则翻看老任务时它们会以原始英文出现在时间轴上。
 */
const PHASE: Record<string, string> = {
  initialize: "准备中",
  queued: "排队中",
  running: "进行中",
  research: "搜集资料",
  verifier: "核验证据",
  composition_pending: "准备综合",
  synthesizing: "研究综合",
  composition: "研究综合完成",
  writing: "撰写报告",
  attributing: "标注出处",
  reviewing: "通读审阅",
  revising: "修订报告",
  verified: "核验通过",
  partial: "部分通过",
  report_failed: "核验未通过",
  rendering: "输出报告",
  report_rendered: "报告已生成",
  // 改版前的阶段，只会出现在旧任务的事件回放里。
  verifying: "逐句核对",
  revisions_exhausted: "修订次数用尽",
  draft_rendered: "报告已生成",
  failed: "失败",
  cancelling: "正在取消",
  cancelled: "已取消",
};

const ERROR: Record<string, string> = {
  research_budget_exhausted_without_evidence: "调查预算用尽，仍未找到足够证据",
  verifier_major_gap: "证据核对发现重大缺口",
  verifier_output_invalid: "证据核对环节返回了无法解析的结果",
  synthesis_contract_error: "研究综合环节返回了无法解析的结果",
  writer_contract_error: "撰写环节返回了无法解析的结果",
  attribution_contract_error: "出处标注环节返回了无法解析的结果",
  review_contract_error: "通读审阅环节返回了无法解析的结果",
  planner_schema_error_limit: "任务规划连续返回无法解析的结果",
  // 改版前的错误码，旧任务的快照里还会出现。
  report_verifier_contract_error: "报告核对环节返回了无法解析的结果",
  draft_render_error: "报告生成失败",
  job_execution_error: "任务执行出错",
  service_unavailable: "模型服务暂时不可用",
  validation_error: "请求内容未通过校验",
  report_not_ready: "报告尚未就绪",
  job_not_found: "找不到这个任务",
  job_not_cancellable: "这个任务已经停了，无法再取消",
  job_not_deletable: "这个任务还在进行中，先取消它才能删除",
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

/** What a finished job produced, e.g. "报告已交付". Empty when still running. */
export const outcomeLabel = (value: string | null | undefined): string => pick(OUTCOME, value);

/** 报告的交付判定，例如"部分核验"。没有判定（还没交付、或旧任务）时是空串。 */
export const verificationLabel = (value: string | null | undefined): string =>
  pick(VERIFICATION, value);

/**
 * One reader-facing phrase for a job's lifecycle status.
 */
export function jobStatusLabel(
  status: string,
  _outcome?: string | null,
  _verification?: string | null,
): string {
  if (status === "queued" || status === "cancelling") return "研究中";
  return pick(STATUS, status);
}

/**
 * The seven steps of the pipeline, named for the person watching them.
 *
 * 后端的阶段比七步多：综合、撰写、标注、审阅各是独立阶段。轨道故意合并成七格，
 * 和 `cli/view.py` 的 `_phase_index` 一致——九格会把轨道挤到横向滚动，而"现在具体
 * 在哪一步"由阶段文案和时间轴负责说清楚。
 */
export const PHASE_STEPS = [
  "确认问题",
  "制定计划",
  "搜集资料",
  "核验证据",
  "综合撰写",
  "出处审阅",
  "输出报告",
] as const;
