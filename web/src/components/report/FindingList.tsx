import type { AttributionFinding, ReadthroughFinding, ReviewFinding } from "../../api/types";

type Props = {
  spans: AttributionFinding[];
  review: ReviewFinding[];
  readthrough: ReadthroughFinding[];
};

const REVIEW_KIND: Record<ReviewFinding["kind"], string> = {
  brief_response: "没有回应研究问题",
  user_constraint: "没有满足用户要求",
  material_omission: "遗漏了实质内容",
  conclusion_integrity: "结论与正文对不上",
};

const READTHROUGH_KIND: Record<ReadthroughFinding["kind"], string> = {
  dangling_reference: "指代落空",
  broken_transition: "行文断裂",
  summary_mismatch: "概述与正文不符",
  orphaned_passage: "段落孤立",
};

/**
 * 没站住的地方。这些以前只存在于审计文件里，读者看不到——而"哪一句没通过、
 * 为什么"恰恰是判定为部分通过时最该让人看见的东西。
 *
 * 跨度级的问题引用原话；整篇级的问题落在段落上，报告正文里没有段落编号可指，
 * 所以只写理由。
 */
export function FindingList({ spans, review, readthrough }: Props) {
  if (!spans.length && !review.length && !readthrough.length) return null;
  return (
    <div className="findings">
      {review.map((finding, index) => (
        <div key={`review-${index}`} className="finding whole">
          <div className="finding-kind">{REVIEW_KIND[finding.kind] ?? finding.kind}</div>
          <p className="finding-reason">{finding.reason}</p>
        </div>
      ))}
      {spans.map((finding) => (
        <div key={finding.finding_id} className="finding">
          <div className="finding-kind">
            {finding.kind === "in_place_downgrade" ? "已就地改写" : "未找到出处"}
          </div>
          <blockquote className="finding-text">{finding.text}</blockquote>
          <p className="finding-reason">{finding.reason}</p>
        </div>
      ))}
      {readthrough.map((finding, index) => (
        <div key={`read-${index}`} className="finding whole">
          <div className="finding-kind">{READTHROUGH_KIND[finding.kind] ?? finding.kind}</div>
          <p className="finding-reason">{finding.reason}</p>
        </div>
      ))}
    </div>
  );
}
