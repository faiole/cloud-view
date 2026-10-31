"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import TopologyDiagram from "@/components/TopologyDiagram";
import { api, PROVIDER_LABEL, type TopoInstance, type Topology } from "@/lib/api";

export default function Topologia() {
  const { id } = useParams<{ id: string }>();
  const [t, setT] = useState<Topology | null>(null);
  const [sel, setSel] = useState<TopoInstance | null>(null);
  useEffect(() => { api<Topology>(`/tenants/${id}/topology`).then(setT); }, [id]);

  return (
    <div className="stack">
      <div className="card inline">
        <span>{t ? `${t.networks.length} redes` : "…"}</span>
        {t && t.exposed > 0 ? <span className="pill bad">{t.exposed} instância(s) exposta(s) à internet</span> : t && <span className="pill ok">nenhuma instância exposta</span>}
        <span className="legend"><i className="lg-exposed" /> porta sensível aberta para a internet <i className="lg-public" /> IP público <i className="lg-pubnet" /> subnet pública</span>
      </div>

      {sel && (
        <div className="card stack-sm">
          <div className="budget-head"><h2>{sel.name || sel.id}</h2><button className="ghost" onClick={() => setSel(null)}>Fechar</button></div>
          <p className="muted">{PROVIDER_LABEL[sel.provider]} · {sel.instance_type} · {sel.state}</p>
          <p>IPs privados: <span className="mono">{sel.private_ips.join(", ") || "—"}</span> · públicos: <span className="mono">{sel.public_ips.join(", ") || "—"}</span></p>
          <p>Regras de acesso: {sel.security_groups.join(", ") || "—"}</p>
          {sel.exposure.length > 0 && <ul className="findings">{sel.exposure.map((x) => <li key={x} className="sev-critical">{x}</li>)}</ul>}
        </div>
      )}

      {(t?.networks ?? []).map((n) => (
        <div className="card" key={n.id}>
          <TopologyDiagram net={n} onSelect={setSel} selected={sel?.id ?? null} />
        </div>
      ))}

      {t && t.unplaced.length > 0 && (
        <div className="card">
          <h2>Instâncias sem rede coletada</h2>
          <ul>{t.unplaced.map((i) => <li key={i.id}>{i.name || i.id} · {PROVIDER_LABEL[i.provider]}</li>)}</ul>
        </div>
      )}
      {t && t.networks.length === 0 && <p className="muted">Nenhuma rede coletada ainda para esta empresa.</p>}
    </div>
  );
}
