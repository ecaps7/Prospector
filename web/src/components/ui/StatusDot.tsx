import { statusTone } from "../../lib/status";

type Props = {
  status: string;
  outcome?: string | null;
  verification?: string | null;
};

export function StatusDot({ status, outcome, verification }: Props) {
  return (
    <span className={`status-dot ${statusTone(status, outcome, verification)}`} aria-hidden="true" />
  );
}
