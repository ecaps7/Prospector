import { StatusDot } from "../ui/StatusDot";

type Props = {
  status: string;
  statusLabel: string;
  question: string;
  onCancel: () => void;
  cancellable: boolean;
};

export function MonitorHead({ status, statusLabel, question, onCancel, cancellable }: Props) {
  return (
    <div className="monitor-head">
      <div className="job-capsule">
        <StatusDot status={status} />
        <span className="job-label">{statusLabel}</span>
        <span className="job-q">{question}</span>
      </div>
      <div className="monitor-actions">
        <button className="btn quiet sm" type="button" onClick={onCancel} disabled={!cancellable}>
          取消任务
        </button>
      </div>
    </div>
  );
}
