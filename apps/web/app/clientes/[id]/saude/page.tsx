"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ago, api, type HealthSummary, type Resource } from "@/lib/api";

function when(iso: string | null) {
  return iso ? new Date(iso).toLocaleString("pt-BR") : "—";
}

export default function Saude() {
  const { id } = useParams<{ id: string }>();
  const [h, setH] = useState<HealthSummary | null>(null);
  const [instances, setInstances] = useState<Resource[]>([]);
  useEffect(() => {
    api<HealthSummary>(`/tenants/${id}/health`).then(setH);
    api<Resource[]>(`/tenants/${id}/resources?type=ec2_instance,vm`).then(setInstances);
  }, [id]);
  type Patch = { managed?: boolean; critical?: number; security?: number; missing?: number; failed?: number; pending_reboot?: number; last_scan?: string };
  const patchOf = (r: Resource) => (r.attributes.patch ?? {}) as Patch;
  const managed = instances.filter((r) => patchOf(r).managed);
  const pending = managed.filter((r) => (patchOf(r).critical ?? 0) + (patchOf(r).security ?? 0) > 0);

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
        <h2>Atualizações (AWS SSM Patch Manager)</h2>
        <p className="muted">{managed.length} de {instances.length} instâncias gerenciadas pelo SSM · {pending.length} com atualizações críticas ou de segurança pendentes.</p>
        {managed.length > 0 && (
          <table>
            <thead><tr><th>Instância</th><th style={{ textAlign: "right" }}>Críticas</th><th style={{ textAlign: "right" }}>Segurança</th><th style={{ textAlign: "right" }}>Faltando</th><th>Reinício pendente</th><th>Última verificação</th></tr></thead>
            <tbody>
              {managed.sort((a, b) => (patchOf(b).critical ?? 0) - (patchOf(a).critical ?? 0)).map((r) => {
                const p = patchOf(r);
                return (
                  <tr key={r.id}>
                    <td><strong>{r.name || r.native_id}</strong><br /><span className="muted mono small">{r.native_id}</span></td>
                    <td className="num">{p.critical ? <span className="pill bad">{p.critical}</span> : 0}</td>
                    <td className="num">{p.security ?? 0}</td>
                    <td className="num">{p.missing ?? 0}</td>
                    <td>{p.pending_reboot ? `${p.pending_reboot} pacotes` : "não"}</td>
                    <td>{p.last_scan ? new Date(p.last_scan).toLocaleString("pt-BR") : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

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
