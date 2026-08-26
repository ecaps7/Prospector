import { Meter } from "../ui/Meter";

export function UsageRow({ name, value, pct, accent }: UsageMetric) {
  return (
    <div className={`usage-row${accent ? " accent" : ""}${pct === undefined ? " unbounded" : ""}`}>
      <div className="top">
        <span className="name">{name}</span>
        <span className="val">{value}</span>
      </div>
      {pct === undefined ? null : <Meter pct={pct} />}
    </div>
  );
}

export type UsageMetric = {
  name: string;
  value: string;
  /** Omit when the metric has no real ceiling — a bar would imply a budget that does not exist. */
  pct?: number;
  accent?: boolean;
};

export function UsagePanel({ metrics }: { metrics: UsageMetric[] }) {
  const bounded = metrics.filter((metric) => metric.pct !== undefined);
  const unbounded = metrics.filter((metric) => metric.pct === undefined);
  return (
    <div className="card usage-panel">
      <div className="panel-title">配额</div>
      {bounded.map((metric) => (
        <UsageRow key={metric.name} {...metric} />
      ))}
      {unbounded.length ? (
        <>
          <div className="panel-title usage-split">累计用量 · 无上限</div>
          <div className="usage-tally">
            {unbounded.map((metric) => (
              <div className="tally-row" key={metric.name}>
                <span className="name">{metric.name}</span>
                <span className="val mono">{metric.value}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}
      <p className="usage-note">
        只有上方两项有硬上限，下方为累计计数。研究何时收尾由证据核对环节判定。
      </p>
    </div>
  );
}
