"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ago, api, can, errorText, money, type Budget, type TenantAccount } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const EMPTY = { name: "", amount: "", currency: "USD", account: "", service: "", tag: "", thresholds: "50, 80, 100", forecast: true };

export default function Orcamentos() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [items, setItems] = useState<Budget[]>([]);
  const [accounts, setAccounts] = useState<TenantAccount[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const canManage = can(me, id, "finops.manage");

  const load = () => api<Budget[]>(`/tenants/${id}/budgets`).then(setItems);
  useEffect(() => {
    load();
    api<TenantAccount[]>(`/tenants/${id}/accounts`).then(setAccounts);
  }, [id]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    const body = {
      name: form.name,
      amount: form.amount,
      currency: form.currency.toUpperCase(),
      accounts: form.account ? [form.account] : [],
      services: form.service ? [form.service] : [],
      tag: form.tag || null,
      thresholds: form.thresholds.split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => x > 0),
      alert_on_forecast: form.forecast,
    };
    try {
      await api(`/tenants/${id}/budgets${editing ? `/${editing}` : ""}`, { method: editing ? "PUT" : "POST", body: JSON.stringify(body) });
      setForm(EMPTY); setEditing(null);
      load();
    } catch (err) {
      setMsg(errorText(err, "Confira os campos: valor maior que zero e limites entre 1 e 500%."));
    }
  }

  function edit(b: Budget) {
    setEditing(b.id);
    setForm({
      name: b.name, amount: b.amount, currency: b.currency, account: b.accounts[0] ?? "", service: b.services[0] ?? "",
      tag: b.tag ?? "", thresholds: b.thresholds.join(", "), forecast: b.alert_on_forecast,
    });
  }

  return (
    <div className="stack">
      {canManage && (
        <form className="card inline" onSubmit={save}>
          <label>Nome<input id="b-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required placeholder="Mensal total" /></label>
          <label>Valor por mês<input id="b-amount" inputMode="decimal" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} required placeholder="5000" /></label>
          <label>Moeda<input id="b-currency" value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })} size={4} /></label>
          <label>Só a conta
            <select id="b-account" value={form.account} onChange={(e) => setForm({ ...form, account: e.target.value })}>
              <option value="">Todas</option>
              {accounts.map((a) => <option key={a.id} value={a.native_id}>{a.name || a.native_id}</option>)}
            </select>
          </label>
          <label>Só o serviço<input id="b-service" value={form.service} onChange={(e) => setForm({ ...form, service: e.target.value })} placeholder="Amazon Elastic Compute Cloud" /></label>
          <label>Só a tag<input id="b-tag" value={form.tag} onChange={(e) => setForm({ ...form, tag: e.target.value })} placeholder="projeto=erp" /></label>
          <label>Avisar em (%)<input id="b-thr" value={form.thresholds} onChange={(e) => setForm({ ...form, thresholds: e.target.value })} size={10} /></label>
          <label className="check"><input id="b-fc" type="checkbox" checked={form.forecast} onChange={(e) => setForm({ ...form, forecast: e.target.checked })} /> Avisar se a previsão passar do valor</label>
          <button type="submit">{editing ? "Salvar" : "Criar orçamento"}</button>
          {editing && <button type="button" className="ghost" onClick={() => { setEditing(null); setForm(EMPTY); }}>Cancelar</button>}
          {msg && <span className="error">{msg}</span>}
        </form>
      )}

      <div className="budgets">
        {items.map((b) => {
          const pct = b.pct ? Number(b.pct) : 0;
          const fpct = b.forecast ? (Number(b.forecast) / Number(b.amount)) * 100 : 0;
          const cls = pct >= 100 ? "bad" : pct >= 80 || fpct >= 100 ? "warn" : "ok";
          return (
            <div className="card stack-sm" key={b.id}>
              <div className="budget-head">
                <h2>{b.name}</h2>
                {canManage && (
                  <span className="actions">
                    <button className="ghost" onClick={() => edit(b)}>Editar</button>
                    <button className="danger" onClick={async () => { await api(`/tenants/${id}/budgets/${b.id}`, { method: "DELETE" }); load(); }}>Excluir</button>
                  </span>
                )}
              </div>
              <div className="meter" aria-label={`${pct.toFixed(0)}% do orçamento`}>
                <div className={`meter-fill ${cls}`} style={{ width: `${Math.min(pct, 100)}%` }} />
                {fpct > pct && <div className="meter-forecast" style={{ left: `${Math.min(fpct, 100)}%` }} title="previsão" />}
                {b.thresholds.filter((t) => t < 100).map((t) => <div key={t} className="meter-tick" style={{ left: `${t}%` }} />)}
              </div>
              <div className="budget-nums">
                <span><strong>{money(b.actual ?? 0, b.currency)}</strong> de {money(b.amount, b.currency)} ({pct.toFixed(0)}%)</span>
                <span className="muted">previsão {money(b.forecast ?? 0, b.currency)}</span>
              </div>
              <span className="muted small">
                {[b.accounts.length ? `conta ${b.accounts.join(", ")}` : null, b.services.length ? b.services.join(", ") : null, b.tag ? `tag ${b.tag}` : null].filter(Boolean).join(" · ") || "todos os custos da empresa"}
                {" · avisos em "}{b.thresholds.join("%, ")}% · calculado {ago(b.last_evaluated_at)}
              </span>
            </div>
          );
        })}
        {items.length === 0 && <p className="muted">Nenhum orçamento. {canManage ? "Crie o primeiro acima." : ""}</p>}
      </div>
    </div>
  );
}
