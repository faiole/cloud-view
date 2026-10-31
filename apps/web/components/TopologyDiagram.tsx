"use client";

import { PROVIDER_LABEL, type TopoInstance, type TopoNetwork } from "@/lib/api";

const COL_W = 210;
const GAP = 16;
const CHIP_H = 46;
const TOP = 96; // espaço da "Internet" acima da rede

/** Uma rede (VPC/VNet/VCN): subnets em colunas, instâncias empilhadas, ligações com a internet. */
export default function TopologyDiagram({ net, onSelect, selected }: { net: TopoNetwork; onSelect: (i: TopoInstance) => void; selected: string | null }) {
  const cols = [...net.subnets, ...(net.loose_instances.length ? [{ id: "loose", name: "sem subnet", cidr: null, public: false, instances: net.loose_instances }] : [])];
  const maxRows = Math.max(1, ...cols.map((c) => c.instances.length));
  const width = Math.max(cols.length, 1) * (COL_W + GAP) + GAP * 3;
  const netH = 80 + maxRows * (CHIP_H + 10) + 20;
  const height = TOP + netH + 10;
  const internetX = width / 2;
  const kind = ({ azure: "VNet", oci: "VCN" } as Record<string, string>)[net.provider] ?? "VPC";

  const chips: { inst: TopoInstance; x: number; y: number }[] = [];
  cols.forEach((c, ci) => {
    c.instances.forEach((inst, ri) => {
      chips.push({ inst, x: GAP * 2 + ci * (COL_W + GAP) + 10, y: TOP + 78 + ri * (CHIP_H + 10) });
    });
  });

  return (
    <div className="topo-wrap">
      <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} role="img" aria-label={`Topologia da rede ${net.name}`} className="topo">
        <g>
          <rect x={internetX - 70} y={8} width={140} height={36} rx={18} className="topo-internet" />
          <text x={internetX} y={31} textAnchor="middle" className="topo-internet-label">Internet</text>
        </g>

        <rect x={GAP} y={TOP} width={width - GAP * 2} height={netH} rx={10} className="topo-net" />

        {cols.map((c, ci) => {
          const x = GAP * 2 + ci * (COL_W + GAP);
          return (
            <g key={c.id}>
              <rect x={x} y={TOP + 48} width={COL_W} height={netH - 58} rx={8} className={c.public ? "topo-subnet public" : "topo-subnet"} />
              <text x={x + 10} y={TOP + 72} className="topo-subnet-title">{c.name}{c.public ? " · pública" : ""}</text>
              {c.cidr && <text x={x + COL_W - 10} y={TOP + 72} textAnchor="end" className="topo-muted">{c.cidr}</text>}
            </g>
          );
        })}

        {cols.map((c, ci) => {
          const pub = c.instances.filter((x) => x.public_ips.length);
          if (!pub.length) return null;
          const exposed = pub.some((x) => x.exposed);
          const cx = GAP * 2 + ci * (COL_W + GAP) + COL_W / 2;
          return (
            <g key={`edge-${c.id}`}>
              <path d={`M${internetX} 44 C ${internetX} ${TOP - 20}, ${cx} ${TOP - 20}, ${cx} ${TOP + 48}`} className={exposed ? "topo-edge exposed" : "topo-edge"} />
              <rect x={cx - 52} y={TOP + 40} width={104} height={16} rx={8} className={exposed ? "topo-badge exposed" : "topo-badge"} />
              <text x={cx} y={TOP + 52} textAnchor="middle" className={exposed ? "topo-badge-text exposed" : "topo-badge-text"}>
                {pub.length} com IP público
              </text>
            </g>
          );
        })}

        <text x={GAP + 12} y={TOP + 22} className="topo-net-title topo-halo">{kind} {net.name}</text>
        <text x={GAP + 12} y={TOP + 38} className="topo-muted topo-halo">{net.cidr ?? ""} · {PROVIDER_LABEL[net.provider] ?? net.provider} · {net.region} · {net.account}</text>

        {chips.map(({ inst, x, y }) => {
          const on = inst.state === "running";
          return (
            <g key={inst.id} className="topo-chip-g" onClick={() => onSelect(inst)} role="button" tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter") onSelect(inst); }}>
              <rect x={x} y={y + 6} width={COL_W - 20} height={CHIP_H} rx={6}
                className={`topo-chip${inst.exposed ? " exposed" : ""}${selected === inst.id ? " selected" : ""}`} />
              <circle cx={x + 12} cy={y + 20} r={4} className={on ? "topo-dot on" : "topo-dot"} />
              <text x={x + 22} y={y + 24} className="topo-chip-title">{(inst.name || inst.id).slice(0, 22)}</text>
              <text x={x + 10} y={y + 42} className="topo-muted">{inst.private_ips[0] ?? ""}{inst.public_ips[0] ? ` · ${inst.public_ips[0]}` : ""}</text>
              {inst.exposed && <text x={x + COL_W - 28} y={y + 24} textAnchor="end" className="topo-warn">exposta</text>}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
