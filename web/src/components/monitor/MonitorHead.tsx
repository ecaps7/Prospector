import { StatusDot } from "../ui/StatusDot";
import { PhaseTrack } from "./PhaseTrack";

type Props = {
  status: string;
  statusLabel: string;
  phaseIndex: number;
  onCancel: () => void;
  cancellable: boolean;
};

/** 状态、阶段轨道、取消按钮挤在同一条上。原来状态胶囊里还重复了一遍研究问题，
 *  但正上方的任务栏一直挂着同一句，删掉不丢信息，省下一整行。 */
export function MonitorHead({ status, statusLabel, phaseIndex, onCancel, cancellable }: Props) {
  return (
    <div className="card monitor-bar">
      <span className="job-state">
        <StatusDot status={status} />
        <span className="job-label">{statusLabel}</span>
      </span>
      <PhaseTrack phaseIndex={phaseIndex} status={status} />
      <button className="btn quiet sm" type="button" onClick={onCancel} disabled={!cancellable}>
        取消任务
      </button>
    </div>
  );
}
