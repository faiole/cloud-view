"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ago, api, type HealthSummary } from "@/lib/api";

function when(iso: string | null) {
  return iso ? new Date(iso).toLocaleString("pt-BR") : "—";
}

export default function Saude() {
  const { id } = useParams<{ id: string }>();
  const [h, setH] = useState<HealthSummary | null>(null);
  useEffect(() => { api<HealthSummary>(`/tenants/${id}/health`).then(setH); }, [id]);

  return (
    <div className="stack">
      <div className="grid">
        <div className="card kpi"><div className="label">Instâncias</div><div className="value">{h?.instances ?? "—"}</div></div>
        <div className="card kpi"><div className="label">Ligadas</div><div className="value">{h?.running ?? "—"}</div></div>
        <div className="card kpi">
          <div className="label">Com falha</div>
          <div className="value">{h ? <span className={`pill ${h.impaired.length ? "bad" : "ok"}`}>{h.impaired.length}</span> : "—"}</div>
        </div>
        <div className="card kpi"><div className="label">Última coleta</div><div className="value small-value">{ago(h?.last_collected_at ?? null)}</div></div>
      </div>

      {h && h.impaired.length > 0 && (
        <div className="card table-wrap">
          <h2>Instâncias com falha no status check</h2>
          <table>
            <thead><tr><th>Instância</th><th>Conta / região</th><th>Instância / host / disco</th></tr></thead>
            <tbody>
              {h.impaired.map((r) => (
                <tr key={r.id}>
                  <td><strong>{r.name || r.native_id}</strong><br /><span className="muted mono">{r.native_id}</span></td>
                  <td className="mono">{r.account_native_id}<br /><span className="muted">{r.region}</span></td>
                  <td>{r.health_details.instance ?? "—"} / {r.health_details.system ?? "—"} / {r.health_details.ebs ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card table-wrap">
        <h2>Eventos abertos e manutenções agendadas</h2>
        <table>
          <thead><tr><th>Evento</th><th>Serviço / região</th><th>Início</th><th>Fim</th><th>Situação</th></tr></thead>
          <tbody>
            {(h?.events ?? []).map((e) => (
              <tr key={e.id}>
                <td><strong>{e.title}</strong>{e.description ? <><br /><span className="muted small">{e.description.slice(0, 240)}</span></> : null}</td>
                <td>{e.service ?? "—"}<br /><span className="muted">{e.region}</span></td>
                <td>{when(e.start_at)}</td>
                <td>{when(e.end_at)}</td>
                <td><span className={`pill ${e.status === "open" ? "bad" : "todo"}`}>{e.status === "open" ? "aberto" : "agendado"}</span></td>
              </tr>
            ))}
            {h && h.events.length === 0 && <tr><td colSpan={5} className="muted">Nenhum evento aberto.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
