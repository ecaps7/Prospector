import type { ReportHealth, ReportVerdict } from "../../api/types";
import { verificationLabel } from "../../lib/labels";

type Props = {
  title: string;
  verdict: ReportVerdict | null;
  health: ReportHealth | null;
  jobId: string;
};

const VERDICT_NOTE: Record<ReportVerdict, string> = {
  verified: "出处标注与通读审阅都没有留下拦截项。",
  partial: "边角处有内容没能找到出处，主结论不受影响；未通过的地方列在下面。",
  failed: "有站不住的内容落在报告的主要判断上。报告仍然交付，但读的时候要带着下面这几条看。",
};

/**
 * 报告页的抬头。判定和数字都由后端算好——`verification_status` 是流水线定的，
 * 核对情况的每个数都来自流水线已有的记录，这里一个都不重算。
 */
export function ReportHead({ title, verdict, health, jobId }: Props) {
  return (
    <div className="report-head">
      <div className="report-title-row">
        <h1>{title}</h1>
        {verdict ? (
          <span className={`vbadge ${verdict === "verified" ? "ok" : "warn"}`}>
            {verdict === "verified" ? "✓" : "⚠"} {verificationLabel(verdict)}
          </span>
        ) : null}
      </div>
      {verdict && verdict !== "verified" ? (
        <div className="report-note">{VERDICT_NOTE[verdict]}</div>
      ) : null}
      {health ? (
        <div className="report-stats">
          <span>
            全文 <b>{health.blocks}</b> 段
          </span>
          <span>
            含已核对事实 <b>{health.checked_blocks}</b> 段
          </span>
          <span>
            含推理 <b>{health.reasoned_blocks}</b> 段
          </span>
          {health.failed_claims ? (
            <span className="warn">
              未通过 <b>{health.failed_claims}</b> 处
            </span>
          ) : null}
          {health.unchecked_spans ? (
            <span className="warn">
              未给出结论 <b>{health.unchecked_spans}</b> 处
            </span>
          ) : null}
          <span>
            材料 <b>{health.assertions_used}</b>/{health.assertions_collected} 条
          </span>
        </div>
      ) : null}
      <div className="report-actions">
        <a className="btn ghost sm" href={`/api/jobs/${jobId}/report?format=md`}>
          下载 Markdown
        </a>
        <a className="btn ghost sm" href={`/api/jobs/${jobId}/report?format=json`} target="_blank" rel="noreferrer">
          查看审计 JSON
        </a>
      </div>
    </div>
  );
}
