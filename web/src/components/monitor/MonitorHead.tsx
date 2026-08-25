import { Segmented } from "../Segmented";
import { StatusDot } from "../ui/StatusDot";

const SPEED_OPTIONS = [
  { value: 1, label: "1×" },
  { value: 2, label: "2×" },
  { value: 4, label: "4×" },
];

type Props = {
  status: string;
  statusLabel: string;
  question: string;
  speed: number;
  onSpeedChange: (speed: number) => void;
  onReplay: () => void;
  canReplay: boolean;
  onCancel: () => void;
  cancellable: boolean;
};

export function MonitorHead({
  status,
  statusLabel,
  question,
  speed,
  onSpeedChange,
  onReplay,
  canReplay,
  onCancel,
  cancellable,
}: Props) {
  return (
    <div className="monitor-head">
      <div className="job-capsule">
        <StatusDot status={status} />
        <span className="job-label">{statusLabel}</span>
        <span className="job-q">{question}</span>
      </div>
      <div className="replay-ctrl">
        <Segmented label="回放速度" value={speed} onChange={onSpeedChange} options={SPEED_OPTIONS} />
        <button className="btn ghost sm" type="button" onClick={onReplay} disabled={!canReplay}>
          重新回放
        </button>
        <button className="btn quiet sm" type="button" onClick={onCancel} disabled={!cancellable}>
          取消任务
        </button>
      </div>
    </div>
  );
}
