type Props = {
  title: string;
  verified: boolean;
  statementCount: number;
  failedCount: number;
  requirementFailureCount: number;
};

export function ReportHead({
  title,
  verified,
  statementCount,
  failedCount,
  requirementFailureCount,
}: Props) {
  const partialLabel = requirementFailureCount
    ? `⚠ 未完全履行任务 · ${requirementFailureCount} 项要求未通过${failedCount ? ` · ${failedCount} 句未通过` : ""}`
    : `⚠ 部分核对 · ${failedCount} 句未通过`;
  return (
    <div className="report-head">
      <div className="report-title-row">
        <h1>{title}</h1>
        <span className={`vbadge ${verified ? "ok" : "warn"}`}>
          {verified
            ? `✓ 已逐句核对 · ${statementCount} 句全部通过`
            : partialLabel}
        </span>
      </div>
      {!verified ? (
        <div className="report-note">
          {requirementFailureCount
            ? `报告在修订次数用尽后仍未完全回答核心问题或满足用户要求，因此不能标记为已验证。${failedCount ? "未通过逐句核对的句子也已在正文中标出。" : ""}引用仍只代表相应句子的事实依据通过核对。`
            : "本报告为部分核对：未通过核对的句子保留原文、不带引用角标并如实标出。"}
        </div>
      ) : null}
    </div>
  );
}
