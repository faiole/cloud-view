"use client";

import { useEffect, useState } from "react";
import AlertList from "@/components/AlertList";
import Channels from "@/components/Channels";
import { api, categoryLabel, type Alert } from "@/lib/api";

export default function AlertasPlataforma() {
  const [status, setStatus] = useState("active");
  const [category, setCategory] = useState("");
  const [items, setItems] = useState<Alert[]>([]);

  const load = () => {
    const q = new URLSearchParams({ status });
    if (category) q.set("category", category);
    api<Alert[]>(`/admin/alerts?${q}`).then(setItems);
  };
  useEffect(() => { load(); }, [status, category]);

  const act = (what: string) => async (a: Alert) => {
    await api(`/admin/alerts/${a.id}/${what}`, { method: "POST" });
    load();
  };
  const counts = items.reduce<Record<string, number>>((acc, a) => ({ ...acc, [a.severity]: (acc[a.severity] ?? 0) + 1 }), {});

  return (
    <div className="stack">
      <div>
        <h1>Alertas</h1>
        <p className="sub">Todas as empresas e a própria plataforma (coletas com erro). Orçamentos, anomalias de custo, saúde, exposição e atualizações.</p>
      </div>
      <div className="grid">
        <div className="card kpi"><div className="label">Críticos</div><div className="value"><span className="pill sev-pill-critical">{counts.critical ?? 0}</span></div></div>
        <div className="card kpi"><div className="label">Atenção</div><div className="value"><span className="pill sev-pill-warning">{counts.warning ?? 0}</span></div></div>
        <div className="card kpi"><div className="label">Informativos</div><div className="value">{counts.info ?? 0}</div></div>
      </div>
      <div className="card inline">
        <label>Situação
          <select id="pal-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="active">Ativos</option><option value="open">Novos</option><option value="acknowledged">Reconhecidos</option><option value="resolved">Resolvidos</option><option value="all">Todos</option>
          </select>
        </label>
        <label>Tipo
          <select id="pal-cat" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">Todos</option>
            {Object.entries(categoryLabel).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      <div className="card"><AlertList items={items} onAck={act("ack")} onResolve={act("resolve")} showTenant /></div>
      <Channels base="/admin" platform />
    </div>
  );
}
