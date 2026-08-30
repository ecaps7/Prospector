import type { ServerEvent } from "../api/types";
import {
  effortLabel,
  errorLabel,
  jobStatusLabel,
  outcomeLabel,
  phaseLabel,
  verificationLabel,
} from "../lib/labels";
import { limitsForEffort } from "./budget";
import type { TimelineDisplayClass, TimelineDisplayEntry } from "./timelineDisplay";

const STOP_REASON_LABELS: Record<string, string> = {
  expected_evidence_satisfied: "证据目标满足",
  budget_exhausted: "工具预算耗尽",
  worker_rounds_exhausted: "调查轮次用尽",
  no_public_evidence: "未发现公开证据",
  low_information_gain: "连续两批未产生新证据",
  repeating_without_progress: "连续重复同一组检索且未落库",
  blocked_by_scope: "受任务范围限制",
  tool_error: "运行时错误",
};

const VERIFIER_TRIGGER_LABELS: Record<string, string> = {
  planner_finish: "规划判定收尾",
  budget_exhausted: "决策轮耗尽",
  synthesis_gap: "研究综合请求补研究",
};

const REJECTION_LABELS: Record<string, string> = {
  over_concurrency: "派发任务超过并发上限",
  schema_error: "输出格式不合法",
  empty_finish: "尚无证据，不能结束研究",
};

function firstLine(value: unknown): string {
  return String(value ?? "")
    .trim()
    .split("\n")[0]
    ?.trim() ?? "";
}

/**
 * 时间轴的行是行，不是段。研究综合的结论有两三百字，整段铺进去会把面板撑成一堵墙
 * ——留第一句，长过 60 字再截断，完整结论去报告里读。
 */
function firstSentence(value: unknown, max = 60): string {
  const line = firstLine(value);
  const stop = line.search(/[。！？]/);
  const head = stop >= 0 ? line.slice(0, stop + 1) : line;
  return head.length > max ? `${head.slice(0, max)}…` : head;
}

function shortId(value: unknown): string {
  return String(value ?? "").slice(0, 8);
}

export type TimelineClass = TimelineDisplayClass;
export type TimelineEntry = TimelineDisplayEntry;

export type TimelineContext = {
  effort: string;
  taskQuestions: Record<string, string>;
  taskOrder: string[];
  researchDecisionsUsed: number;
};

function taskLabel(ctx: TimelineContext, taskId: string): string {
  const index = ctx.taskOrder.indexOf(taskId);
  if (index >= 0) return `T${index + 1}`;
  ctx.taskOrder.push(taskId);
  return `T${ctx.taskOrder.length}`;
}

function classify(eventType: string, text: string): TimelineClass {
  if (
    eventType === "planner.started" ||
    eventType === "planner.decided" ||
    eventType === "planner.rejected" ||
    eventType === "replan.triggered"
  ) {
    return "planner";
  }
  // 工具失败仍是高频噪声（403/404 很常见），不升成 gap。
  if (eventType === "task.tool_used") return "tool";
  if (eventType === "task.round_advanced") return "round";
  if (eventType === "task.evidence_saved") return "evidence";
  if (eventType === "task.finished") return "done";
  if (eventType === "verifier.completed") {
    return text.includes("不通过") || text.includes("失败") ? "gap" : "evidence";
  }
  if (eventType === "synthesis.completed") return text.includes("补研究") ? "gap" : "evidence";
  if (
    eventType === "job.phase_changed" ||
    eventType === "job.stopped" ||
    eventType === "brief.confirmed" ||
    eventType === "report.draft_rendered"
  ) {
    return "phase";
  }
  return "";
}

function tagFor(eventType: string, text: string, taskLabelValue: string | null): string {
  if (eventType.startsWith("planner")) return text.match(/^\[(轮 \d+)\]/)?.[1] ?? "规划";
  if (eventType === "replan.triggered") return "重规划";
  if (eventType === "verifier.completed") return "核验";
  if (eventType.startsWith("task.")) return taskLabelValue ?? "任务";
  if (eventType === "report.draft_rendered") return "成文";
  if (eventType === "job.stopped") return "结束";
  return "研究";
}

function remaining(ctx: TimelineContext): number {
  return Math.max(0, limitsForEffort(ctx.effort).decisionRoundLimit - ctx.researchDecisionsUsed);
}

function trackDecisions(ctx: TimelineContext, payload: Record<string, unknown>): void {
  const reported = payload.research_decisions_used;
  if (reported == null) ctx.researchDecisionsUsed += 1;
  else ctx.researchDecisionsUsed = Number(reported);
}

function renderLines(ctx: TimelineContext, event: ServerEvent): string[] {
  const payload = event.payload;
  const eventType = event.event_type;

  if (eventType === "brief.confirmed") {
    return [`[研究] 研究方案已确认（${effortLabel(String(payload.effort ?? "")) || "未知档位"}）`];
  }
  if (eventType === "job.phase_changed") {
    return renderPhase(payload);
  }
  if (eventType === "planner.started") {
    return [`[轮 ${Number(payload.decision_round ?? 0)}] 正在制定研究计划`];
  }
  if (eventType === "planner.decided") {
    return renderPlanner(ctx, payload);
  }
  if (eventType === "planner.rejected") {
    const round = Number(payload.decision_round ?? 0);
    const reasonCode = String(payload.reason_code ?? "");
    const reason = REJECTION_LABELS[reasonCode] ?? reasonCode;
    if (reasonCode === "schema_error") {
      return [`[轮 ${round}] 规划输出格式不合法，重试（不计研究轮）`];
    }
    trackDecisions(ctx, payload);
    return [`[轮 ${round}] 规划决策被拒绝：${reason}（余 ${remaining(ctx)} 轮）`];
  }
  if (eventType === "verifier.completed") {
    return renderVerifier(ctx, payload);
  }
  if (eventType === "synthesis.completed") {
    const summary = firstSentence(payload.synthesis);
    const head =
      payload.decision === "needs_research" ? "研究综合请求补研究" : "研究综合完成";
    return [`[综合] ${head}${summary ? `：${summary}` : ""}`];
  }
  if (eventType === "replan.triggered") {
    return [`[重规划] 证据核对未放行，改出研究计划 第 ${payload.plan_version} 版`];
  }
  if (eventType === "report.draft_rendered") {
    // 交付判定只在这条事件里，phase=report_rendered 那条什么都不多说，所以让这条
    // 独占收尾行，`renderPhase` 对渲染完成的阶段返回空。
    const verdict = verificationLabel(String(payload.verification_status ?? ""));
    return [`[成文] 报告已交付${verdict ? `（${verdict}）` : ""}`];
  }
  if (eventType === "job.stopped") {
    return renderStopped(payload);
  }

  const taskId = String(payload.task_id ?? event.task_id ?? "");
  if (!taskId) return [];
  const label = taskLabel(ctx, taskId);

  if (eventType === "task.started") {
    const budget = (payload.budget as { max_worker_rounds?: number } | undefined) ?? {};
    return [`[${label}] 开始调查（调查轮次预算 ${budget.max_worker_rounds ?? 0} 轮）`];
  }
  if (eventType === "task.tool_used") {
    return renderTool(label, payload);
  }
  if (eventType === "task.evidence_saved") {
    const assertions = Array.isArray(payload.assertion_ids) ? payload.assertion_ids.length : 0;
    return [`[${label}] 落证 ${assertions} 条断言（${Number(payload.excerpt_count ?? 0)} 段原文）`];
  }
  if (eventType === "task.finished") {
    const reasonCode = String(payload.stop_reason ?? "");
    const reason = STOP_REASON_LABELS[reasonCode] ?? reasonCode;
    const used = Number(payload.rounds_used ?? 0);
    const limit = Number(payload.rounds_limit ?? 0);
    const tools = Number(payload.tool_calls_used ?? 0);
    const assertions = Number(payload.assertion_count ?? 0);
    return [`[${label}] 收工：${reason}（轮 ${used}/${limit}，工具 ${tools} 次，累计断言 ${assertions} 条）`];
  }
  if (eventType === "task.round_advanced") {
    return [
      `[${label}] 完成第 ${Number(payload.rounds_used ?? 0)}/${Number(payload.rounds_limit ?? 0)} 轮调查`,
    ];
  }
  return [];
}

function renderPhase(payload: Record<string, unknown>): string[] {
  const phase = String(payload.phase ?? "");
  if (phase === "research") return ["[研究] 开始"];
  if (phase === "verifier") {
    // 核验现在有三个触发口，其中"研究综合请求补研究"发生在研究已经放行之后——
    // 不写出来的话，时间轴上会莫名其妙地第二次"研究阶段结束"。
    const trigger = String(payload.trigger ?? "");
    const label = VERIFIER_TRIGGER_LABELS[trigger] ?? trigger ?? "";
    const planVersion = payload.plan_version;
    const plan = planVersion == null ? "" : `研究计划 第 ${planVersion} 版，`;
    const note = label ? `（${plan}触发：${label}）` : "";
    return [`[研究] 研究阶段结束，等待核验${note}`];
  }
  if (phase === "composition_pending") return ["[综合] 证据核对已放行，等待研究综合"];
  if (phase === "synthesizing") return ["[综合] 正在整合研究材料"];
  if (phase === "composition") return ["[成文] 研究综合完成，开始写作"];
  if (phase === "writing") return ["[成文] 正在组织深度研究报告"];
  if (phase === "attributing") return ["[成文] 正在为正文寻找出处"];
  if (phase === "reviewing") return ["[成文] 正在通读全文审阅"];
  if (phase === "revising") return ["[成文] 存在未获支持的表述，正在修订"];
  if (phase === "verified") return ["[成文] 出处与审阅全部通过"];
  if (phase === "partial") return ["[成文] 部分内容未获事实支持，报告仍会交付"];
  if (phase === "report_failed") return ["[成文] 核验未通过，报告仍会随核验结论交付"];
  if (phase === "rendering") return ["[成文] 正在渲染最终报告"];
  // 渲染完成的阶段事件和 report.draft_rendered 同秒到达，说的是同一件事——
  // 交给带判定的那一条去说，这里出行只会连着出现两句"报告渲染完成"。
  if (phase === "report_rendered" || phase === "draft_rendered") return [];
  // 改版前的阶段，只在旧任务的事件回放里出现。
  if (phase === "verifying") return ["[核验] Report Verifier 正在逐句验证"];
  if (phase === "revisions_exhausted") return ["[成文] 修订轮次已用尽，仍有未通过语句；报告将标记为部分通过"];
  // 终态只由 job.stopped 出一行：`cancelled` / `failed` 的 phase 事件后面永远
  // 紧跟一条 job.stopped，两边都渲染就会连着出现两行"已取消"。
  if (phase === "cancelled" || phase === "failed") return [];
  if (phase === "cancelling") return [`[研究] ${phaseLabel(phase)}`];
  return [];
}

/**
 * 收尾行。取消或失败时 status / phase / outcome 三个字段是同一个词，成功收尾时
 * phase 和 outcome 同为 `report_rendered`——去重要按原始取值来，因为阶段表和收尾表
 * 对同一个词给的说法并不相同（"报告已生成" / "报告已交付"），按文案去重拦不住。
 * 失败再补上具体原因。
 */
function renderStopped(payload: Record<string, unknown>): string[] {
  const seen = new Set<string>();
  const labels: string[] = [];
  const push = (raw: string, label: string): void => {
    if (!raw || !label || seen.has(raw)) return;
    seen.add(raw);
    labels.push(label);
  };
  const status = String(payload.status ?? "");
  push(status, jobStatusLabel(status));
  push(String(payload.phase ?? ""), phaseLabel(String(payload.phase ?? "")));
  push(String(payload.outcome ?? ""), outcomeLabel(String(payload.outcome ?? "")));
  const head = labels.join(" · ");
  const reason = errorLabel(String(payload.error_code ?? ""));
  return [`[结束] ${head}${reason ? `：${reason}` : ""}`];
}

function renderPlanner(ctx: TimelineContext, payload: Record<string, unknown>): string[] {
  const round = Number(payload.decision_round ?? 0);
  const decision = String(payload.decision ?? "");
  trackDecisions(ctx, payload);
  if (decision === "dispatch") {
    const taskIds = (payload.task_ids as string[] | undefined) ?? [];
    const reason = firstLine(payload.reason);
    const suffix = reason ? `：${reason}` : "";
    const lines = [
      `[轮 ${round}] 派发 ${taskIds.length} 个调查任务（研究计划 第 ${payload.plan_version} 版，余 ${remaining(ctx)} 轮）${suffix}`,
    ];
    taskIds.forEach((id, index) => {
      const branch = index === taskIds.length - 1 ? "└─" : "├─";
      const label = taskLabel(ctx, id);
      const question = ctx.taskQuestions[id] ?? "";
      lines.push(`  ${branch} ${label} ${firstLine(question)}`);
    });
    return lines;
  }
  if (decision === "finish") return [`[轮 ${round}] 判定可以收尾：${firstLine(payload.reason)}`];
  return [];
}

function renderVerifier(ctx: TimelineContext, payload: Record<string, unknown>): string[] {
  const reported = payload.research_decisions_used;
  if (reported != null) ctx.researchDecisionsUsed = Number(reported);
  const planVersion = payload.plan_version;
  const major = Number(payload.major_gap_count ?? 0);
  if (payload.release_decision === "pass") {
    return [`[核验] 研究计划 第 ${planVersion} 版 通过（重大缺口 ${major} 个）`];
  }
  return [
    `[核验] 研究计划 第 ${planVersion} 版 不通过：${major} 个重大缺口（余 ${remaining(ctx)} 轮）`,
  ];
}

function renderTool(label: string, payload: Record<string, unknown>): string[] {
  const tool = String(payload.tool ?? "未知工具");
  if (payload.error) {
    const head = String(payload.error).trim().split("\n")[0] ?? "";
    return [`[${label}] ${tool} 失败：${head}`];
  }
  if (tool === "web_search") {
    return [`[${label}] 搜索 "${payload.query ?? ""}" → ${Number(payload.result_count ?? 0)} 条结果`];
  }
  if (tool === "web_fetch") {
    return [`[${label}] 网页快照已保存 ${payload.url ?? ""} → ${shortId(payload.doc_id)}`];
  }
  if (tool === "save_findings" && Number(payload.result_count ?? 0) === 0) {
    return [`[${label}] 落证：未产生新证据`];
  }
  if (tool === "save_findings") {
    return [`[${label}] 保存研究发现 → ${Number(payload.result_count ?? 0)} 条结果`];
  }
  return [];
}

export function renderEvent(ctx: TimelineContext, event: ServerEvent): TimelineEntry[] {
  return renderLines(ctx, event).map((line) => {
    const match = line.match(/^\[([^\]]+)\]\s*(.*)$/);
    const tag = match?.[1] ?? tagFor(event.event_type, line, null);
    const text = match?.[2] ?? line;
    return {
      eventId: event.id,
      createdAt: event.created_at ?? "",
      tag,
      text,
      cls: classify(event.event_type, line),
    };
  });
}
