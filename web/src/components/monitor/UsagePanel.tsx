import { Meter } from "../ui/Meter";

export function UsageRow({
  name,
  value,
  pct,
  accent,
}: {
  name: string;
  value: string;
  pct: number;
  accent?: boolean;
}) {
  return (
    <div className={`usage-row${accent ? " accent" : ""}`}>
      <div className="top">
        <span className="name">{name}</span>
        <span className="val">{value}</span>
      </div>
      <Meter pct={pct} />
    </div>
  );
}

export type UsageMetric = {
  name: string;
  value: string;
  pct: number;
  accent?: boolean;
};

export function UsagePanel({ metrics }: { metrics: UsageMetric[] }) {
  return (
    <div className="card usage-panel">
      <div className="panel-title">限额与用量</div>
      {metrics.map((metric) => (
        <UsageRow key={metric.name} {...metric} />
      ))}
      <p className="usage-note">
        Token 与工具栏为相对展示，不构成硬上限。停止研究仍须经过 Research Verifier。
      </p>
    </div>
  );
}
