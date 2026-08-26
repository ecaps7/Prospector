type Props = {
  title: string;
  verified: boolean;
  statementCount: number;
  failedCount: number;
};

export function ReportHead({
  title,
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
            : `⚠ 部分核对 · ${failedCount} 句未通过`}
        </span>
      </div>
      {!verified ? (
        <div className="report-note">
          本报告为部分核对：未通过核对的句子保留原文、不带引用角标并如实标出——与其硬凑一个干净结论，不如让你看清哪些句子没站住。
        </div>
      ) : null}
    </div>
  );
}
