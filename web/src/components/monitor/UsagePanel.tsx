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
      <div className="usage-bounded">
        {bounded.map((metric) => (
          <UsageRow key={metric.name} {...metric} />
        ))}
      </div>
      {/* 原来这里还有一行“累计用量 · 无上限”小标题，纯装饰性地占掉 60px：
          有没有进度条已经把两类指标分开了，换成一条细分隔线就够。 */}
      {unbounded.length ? (
        <div className="usage-tally">
          {unbounded.map((metric) => (
            <div className="tally-row" key={metric.name}>
              <span className="name">{metric.name}</span>
              <span className="val mono">{metric.value}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
