"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, can, effortLabel, errorText, money, recActionLabel, type Coverage, type RecommendationSummary } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const STATUS_LABEL: Record<string, string> = { open: "Abertas", dismissed: "Dispensadas", done: "Feitas", resolved: "Resolvidas", all: "Todas" };

export default function Otimizacao() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [status, setStatus] = useState("open");
  const [action, setAction] = useState("");
  const [data, setData] = useState<RecommendationSummary | null>(null);
  const [coverage, setCoverage] = useState<Coverage[]>([]);
  const [msg, setMsg] = useState("");

  const load = () => {
    const q = new URLSearchParams({ status });
    if (action) q.set("action_type", action);
    api<RecommendationSummary>(`/tenants/${id}/recommendations?${q}`).then(setData);
  };
  useEffect(() => { load(); }, [id, status, action]);
  useEffect(() => { api<Coverage[]>(`/tenants/${id}/commitments`).then(setCoverage).catch(() => setCoverage([])); }, [id]);

  const canManage = can(me, id, "finops.manage") || can(me, id, "actions.execute");

  async function setRec(recId: string, s: string) {
    try {
      await api(`/tenants/${id}/recommendations/${recId}`, { method: "PATCH", body: JSON.stringify({ status: s }) });
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  const last = coverage[coverage.length - 1];

  return (
    <div className="stack">
      <div className="grid">
        <div className="card kpi">
          <div className="label">Economia possível por mês</div>
          <div className="value">{data?.savings.length ? data.savings.map((s) => money(s.cost, s.currency)).join(" + ") : "—"}</div>
        </div>
        <div className="card kpi"><div className="label">Recomendações abertas</div><div className="value">{data?.open ?? "—"}</div></div>
        <div className="card kpi">
          <div className="label">Cobertura de Savings Plans</div>
          <div className="value">{last?.coverage_pct ? `${last.coverage_pct}%` : "—"}</div>
          {last && <div className="muted small">{money(last.on_demand)} ainda sob demanda em {last.month.slice(0, 7)}</div>}
        </div>
      </div>

      {data && Object.keys(data.by_action).length > 0 && (
        <div className="chips">
          <button className={`chip ${action === "" ? "chip-on" : ""}`} onClick={() => setAction("")}>Todas</button>
          {Object.entries(data.by_action).map(([k, n]) => (
            <button key={k} className={`chip ${action === k ? "chip-on" : ""}`} onClick={() => setAction(k)}>{recActionLabel[k] ?? k} <b>{n}</b></button>
          ))}
        </div>
      )}

      <div className="card inline">
        <label>Situação
          <select id="rec-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <span className="muted">Fontes: AWS Cost Optimization Hub e regras do Cloud View (valores com * são estimativas por preço de tabela).</span>
        {msg && <span className="error">{msg}</span>}
      </div>

      <div className="card table-wrap">
        <table>
          <thead><tr><th>Recomendação</th><th>Hoje → sugerido</th><th>Conta / região</th><th>Esforço</th><th style={{ textAlign: "right" }}>Economia/mês</th>{canManage && <th />}</tr></thead>
          <tbody>
            {(data?.items ?? []).map((r) => (
              <tr key={r.id}>
                <td><strong>{r.title}</strong><br /><span className="muted small">{recActionLabel[r.action_type] ?? r.action_type}{r.restart_needed ? " · exige reinício" : ""}{r.status_by && r.status !== "open" ? ` · ${STATUS_LABEL[r.status]?.toLowerCase()} por ${r.status_by}` : ""}</span></td>
                <td className="small">{r.current ?? "—"}<br /><span className="muted">→ {r.recommended ?? "—"}</span></td>
                <td className="mono small">{r.account_native_id ?? "—"}<br /><span className="muted">{r.region}</span></td>
                <td>{r.effort ? effortLabel[r.effort] ?? r.effort : "—"}</td>
                <td className="num"><strong>{money(r.monthly_savings, r.currency)}</strong>{r.estimated ? "*" : ""}{r.savings_pct ? <><br /><span className="muted small">{Number(r.savings_pct).toFixed(0)}%</span></> : null}</td>
                {canManage && (
                  <td className="actions">
                    {r.status === "open" ? (
                      <>
                        <button className="ghost" onClick={() => setRec(r.id, "done")}>Feito</button>
                        <button className="ghost" onClick={() => setRec(r.id, "dismissed")}>Dispensar</button>
                      </>
                    ) : r.status !== "resolved" ? (
                      <button className="ghost" onClick={() => setRec(r.id, "open")}>Reabrir</button>
                    ) : null}
                  </td>
                )}
              </tr>
            ))}
            {data && data.items.length === 0 && <tr><td colSpan={6} className="muted">Nenhuma recomendação {STATUS_LABEL[status].toLowerCase()}.</td></tr>}
          </tbody>
        </table>
      </div>

      {coverage.length > 0 && (
        <div className="card table-wrap">
          <h2>Cobertura de Savings Plans por mês</h2>
          <table>
            <thead><tr><th>Mês</th><th style={{ textAlign: "right" }}>Coberto</th><th style={{ textAlign: "right" }}>Sob demanda</th><th style={{ textAlign: "right" }}>Cobertura</th></tr></thead>
            <tbody>
              {coverage.map((c) => (
                <tr key={c.month}><td>{c.month.slice(0, 7)}</td><td className="num">{money(c.covered)}</td><td className="num">{money(c.on_demand)}</td><td className="num">{c.coverage_pct ? `${c.coverage_pct}%` : "—"}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
