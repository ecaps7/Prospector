import { PHASE_LABELS } from "../state/jobView";

type Props = {
  phaseIndex: number;
  status: string;
};

export function PhaseTrack({ phaseIndex, status }: Props) {
  const completed = status === "completed";
  return (
    <div className="card phase-track" aria-label="研究阶段轨道">
      {PHASE_LABELS.map((label, index) => {
        const done = completed || index < phaseIndex;
        const current = !completed && index === phaseIndex;
        const failed = status === "failed" && current;
        const classes = ["phase", done ? "done" : "", current ? "current" : "", failed ? "failed" : ""]
          .filter(Boolean)
          .join(" ");
        return (
          <span key={label} style={{ display: "contents" }}>
            {index > 0 ? <div className={`phase-line${done || (current && index <= phaseIndex) ? " done" : ""}`} /> : null}
            <div className={classes}>
              <span className="dot">{done ? "✓" : failed ? "!" : ""}</span>
              <span className="lab">{label}</span>
            </div>
          </span>
        );
      })}
    </div>
  );
}
