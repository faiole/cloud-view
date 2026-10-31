import { money } from "@/lib/api";

type Point = { date: string; currency: string; cost: string };

/** Série diária em SVG (uma moeda). */
export default function DailyChart({ points }: { points: Point[] }) {
  if (points.length < 2) return null;
  const currency = points[0].currency;
  const pts = points.filter((p) => p.currency === currency);
  const W = 640, H = 140, P = 8;
  const max = Math.max(...pts.map((p) => Number(p.cost))) || 1;
  const x = (i: number) => P + (i / (pts.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - (v / max) * (H - 2 * P - 14);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(Number(p.cost)).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Custo diário, máximo ${money(max, currency)}`}>
        <line x1={P} x2={W - P} y1={H - P} y2={H - P} className="axis" />
        <path d={`${line} L${x(pts.length - 1)} ${H - P} L${P} ${H - P} Z`} className="area" />
        <path d={line} className="line" />
        <circle cx={x(pts.length - 1)} cy={y(Number(last.cost))} r={3.5} className="dot" />
        <text x={P} y={12} className="tick">{money(max, currency)}</text>
      </svg>
      <div className="chart-foot"><span>{pts[0].date}</span><span>{last.date}</span></div>
    </div>
  );
}
