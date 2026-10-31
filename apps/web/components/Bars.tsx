import { money, type UsageRow } from "@/lib/api";

/** Barras horizontais proporcionais ao maior valor (todas na mesma moeda). */
export default function Bars({ rows, max = 12 }: { rows: UsageRow[]; max?: number }) {
  const shown = rows.slice(0, max);
  const top = Math.max(...shown.map((r) => Number(r.cost)), 0) || 1;
  return (
    <div className="bars">
      {shown.map((r) => (
        <div className="bar" key={r.key + r.currency}>
          <span className="bar-label" title={r.label}>{r.label}</span>
          <div className="track"><div className="fill" style={{ width: `${(Number(r.cost) / top) * 100}%` }} /></div>
          <span className="bar-value">{money(r.cost, r.currency)}</span>
        </div>
      ))}
      {shown.length === 0 && <p className="muted">Sem custos no período.</p>}
    </div>
  );
}
