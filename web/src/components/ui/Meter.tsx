type Props = {
  /** 0–100; clamped, and NaN/Infinity collapse to 0. */
  pct: number;
};

/**
 * The track/fill pair. Colour and size come from the parent block
 * (`.usage-row`, `.rounds-bar`, …), so this only owns the geometry.
 */
export function Meter({ pct }: Props) {
  const width = Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
  return (
    <div className="track">
      <div className="fill" style={{ width: `${width}%` }} />
    </div>
  );
}
