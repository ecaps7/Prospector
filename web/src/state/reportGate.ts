import type { JobDetail } from "../api/types";
import { fmtClock } from "../lib/format";
import { errorLabel, phaseLabel } from "../lib/labels";
import { phaseIndex } from "./jobView";

/**
 * 后端只有一个 `report_not_ready`（409）：报告文件不在，就是这一句。可"报告不在"
 * 底下压着四种完全不同的现实——还在查资料、正在写、被取消了、跑失败了——把它们
 * 都渲染成同一句话，等于什么都没说。这里按任务快照把那一句重新拆开。
 *
 * 判据是 `report.json_ref`，和报告接口自己查的那一列是同一列，所以这里说"就绪"
 * 时接口必定拿得到文件。
 */
export type ReportGateKind =
  | "ready"
  /** 还没进撰写阶段：报告根本还不存在，别拿骨架屏骗人。 */
  | "researching"
  /** 已在撰写／核对：报告马上就有，可以先摆出版式。 */
  | "composing"
  | "cancelling"
  | "cancelled"
  | "failed"
  /** 任务收尾了却没落下报告文件——这才是真的异常。 */
  | "missing";

export type ReportGate = { kind: ReportGateKind };

/** `composition_pending` 在阶段轨道上的位置，也就是"开始产出报告"的那一刀。 */
const COMPOSITION_STEP = 4;

export function reportGate(job: JobDetail): ReportGate {
  // 报告有时先于任务收尾落库，那就已经能读了——不必等状态翻成 completed。
  if (job.report?.json_ref) return { kind: "ready" };
  if (job.status === "failed") return { kind: "failed" };
  if (job.status === "cancelled") return { kind: "cancelled" };
  if (job.status === "cancelling") return { kind: "cancelling" };
  if (job.status === "completed") return { kind: "missing" };
  return { kind: phaseIndex(job.phase) >= COMPOSITION_STEP ? "composing" : "researching" };
}

/**
 * 这份快照还会不会变。报告页据此决定要不要转圈，App 据此决定要不要接着轮询——
 * 终态一到就都停下，页面变成一个说得清原因的死胡同。
 */
export function isGateLive(kind: ReportGateKind): boolean {
  return kind === "researching" || kind === "composing" || kind === "cancelling";
}

/** 阶段字段有时装的是任务状态而不是真的阶段，那就没有"停在哪一步"可说。 */
const STATUS_PHASES = new Set(["initialize", "queued", "running", "cancelling", "cancelled", "failed"]);

/**
 * 任务停在哪一步。终态的快照里 `phase` 往往只剩 `failed`／`cancelled` 这种状态词，
 * 那就是"这份快照说不出停在哪"——空字符串，调用方据此把阶段轨道也一并收起来，
 * 而不是让它把红叉画在第一步上。
 */
export function gateStageName(job: JobDetail): string {
  return STATUS_PHASES.has(job.phase) ? "" : phaseLabel(job.phase);
}

/** 撰写那几步各有各的说法，"正在修订次数用尽"这种话不能让它拼出来。 */
function composingTitle(phase: string): string {
  if (phase === "verifying") return "正在逐句核对初稿";
  if (phase === "revising") return "正在按核对结果修订初稿";
  if (phase === "writing" || phase === "composition_pending") return "正在撰写初稿";
  return "正在生成报告";
}

export type ReportGateCopy = {
  /** 主句：眼下在发生什么，或者为什么不会再有报告。 */
  title: string;
  /** 辅助句：停在哪一步、跑了多久、还能去哪儿看。 */
  detail: string;
};

/**
 * 每种等待态说自己的话。原来这里六种情况共用一句"报告尚未就绪"，
 * 既没说在等什么，也没说值不值得等。
 */
export function reportGateCopy(job: JobDetail, kind: ReportGateKind, elapsed: number): ReportGateCopy {
  const stage = gateStageName(job);
  const ran = `已运行 ${fmtClock(elapsed)}`;
  switch (kind) {
    case "researching":
      return {
        title: job.status === "queued" ? "任务在排队，还没开始查资料" : "还在搜集资料，报告要等研究结束后才动笔",
        detail: [stage ? `当前：${stage}` : "", ran].filter(Boolean).join(" · "),
      };
    case "composing":
      return {
        title: composingTitle(job.phase),
        detail: [stage ? `当前：${stage}` : "", ran].filter(Boolean).join(" · "),
      };
    case "cancelling":
      return {
        title: "任务正在取消，报告不会再生成",
        detail: [stage ? `停在：${stage}` : "", ran].filter(Boolean).join(" · "),
      };
    case "cancelled":
      return {
        title: "研究已取消，没有生成报告",
        detail: [stage ? `停在：${stage}` : "", "已经收集到的证据仍留在研究监控里"].filter(Boolean).join(" · "),
      };
    case "failed":
      return {
        title: errorLabel(job.error_code) || "研究失败，没有生成报告",
        detail: [stage ? `失败于：${stage}` : "", "研究监控里的时间线能看到停在哪一步"]
          .filter(Boolean)
          .join(" · "),
      };
    case "missing":
      return {
        title: "研究已结束，但没有留下报告文件",
        detail: errorLabel(job.error_code) || "这属于异常，请到研究监控核对最后几条事件",
      };
    case "ready":
      return { title: "", detail: "" };
  }
}
