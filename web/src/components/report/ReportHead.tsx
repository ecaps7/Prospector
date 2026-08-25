type Props = {
  title: string;
  verificationStatus: string;
  verified: boolean;
  statementCount: number;
  failedCount: number;
};

export function ReportHead({
  title,
  verificationStatus,
  verified,
  statementCount,
  failedCount,
}: Props) {
  return (
    <div className="report-head">
      <div className="report-title-row">
        <h1>{title}</h1>
        <span className={`vbadge ${verified ? "ok" : "warn"}`}>
          {verified
            ? `✓ 已逐句核对 · ${statementCount} 句全部通过`
            : `⚠ 部分核对 · ${failedCount} 句未通过（partial）`}
        </span>
      </div>
      {!verified ? (
        <div className="report-note">
          本报告标记为 {verificationStatus}：未通过核对的句子保留原文、不带引用角标并如实标出——与其硬凑一个干净结论，不如如实标出哪些句子没通过核对。
        </div>
      ) : null}
    </div>
  );
}
