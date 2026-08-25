import { statusTone } from "../../lib/status";

type Props = {
  status: string;
  outcome?: string | null;
};

export function StatusDot({ status, outcome }: Props) {
  return <span className={`status-dot ${statusTone(status, outcome)}`} aria-hidden="true" />;
}
