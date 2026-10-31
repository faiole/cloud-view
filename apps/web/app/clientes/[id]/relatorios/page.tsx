"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import Bars from "@/components/Bars";
import DailyChart from "@/components/DailyChart";
import { api, money, type TenantAccount, type UsageReport } from "@/lib/api";

function iso(d: Date) {
  return d.toISOString().slice(0, 10);
}

export default function Relatorios() {
  const { id } = useParams<{ id: string }>();
  const today = new Date();
  const [start, setStart] = useState(iso(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [end, setEnd] = useState(iso(today));
  const [group, setGroup] = useState("service");
  const [account, setAccount] = useState("");
  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [tagKeys, setTagKeys] = useState<string[]>([]);
  const [tagValues, setTagValues] = useState<string[]>([]);
  const [accounts, setAccounts] = useState<TenantAccount[]>([]);
  const [rep, setRep] = useState<UsageReport | null>(null);

  useEffect(() => {
    api<{ keys: string[] }>(`/tenants/${id}/tags`).then((r) => setTagKeys(r.keys));
    api<TenantAccount[]>(`/tenants/${id}/accounts`).then(setAccounts);
  }, [id]);

  useEffect(() => {
    setTagValue("");
    if (tagKey) api<{ values: string[] }>(`/tenants/${id}/tags/${encodeURIComponent(tagKey)}`).then((r) => setTagValues(r.values));
    else setTagValues([]);
  }, [id, tagKey]);

  const query = useMemo(() => {
    const q = new URLSearchParams({ start, end });
    if (account) q.append("account", account);
    if (tagKey && tagValue) q.set("tag", `${tagKey}=${tagValue}`);
    return q;
  }, [start, end, account, tagKey, tagValue]);

  useEffect(() => {
    const q = new URLSearchParams(query);
    q.set("group_by", group);
    api<UsageReport>(`/tenants/${id}/reports/usage?${q}`).then(setRep).catch(() => setRep(null));
  }, [id, query, group]);

  const month = start.slice(0, 7);

  return (
    <div className="stack">
      <form className="card inline" onSubmit={(e) => e.preventDefault()}>
        <label>De<input id="r-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label>Até<input id="r-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
        <label>Agrupar por
          <select id="r-group" value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="service">Serviço</option>
            <option value="account">Conta</option>
            <option value="region">Região</option>
            <option value="month">Mês</option>
            <option value="day">Dia</option>
            <option value="provider">Nuvem</option>
            {tagKeys.map((k) => <option key={k} value={`tag:${k}`}>Tag: {k}</option>)}
          </select>
        </label>
        <label>Conta
          <select id="r-account" value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="">Todas</option>
            {accounts.map((a) => <option key={a.id} value={a.native_id}>{a.name || a.native_id}</option>)}
          </select>
        </label>
        <label>Filtrar por tag
          <select id="r-tagkey" value={tagKey} onChange={(e) => setTagKey(e.target.value)}>
            <option value="">—</option>
            {tagKeys.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        {tagKey && (
          <label>Valor
            <select id="r-tagvalue" value={tagValue} onChange={(e) => setTagValue(e.target.value)}>
              <option value="">Todos</option>
              {tagValues.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
        )}
      </form>

      <div className="grid">
        {(rep?.totals ?? []).map((t) => (
          <div className="card kpi" key={t.currency}><div className="label">Total no período ({t.currency})</div><div className="value">{money(t.cost, t.currency)}</div></div>
        ))}
        <div className="card stack-sm">
          <div className="label-sm">Exportar</div>
          <a className="button" href={`/api/tenants/${id}/reports/usage.csv?${query}`}>Detalhe diário (CSV)</a>
          <a className="button ghost" href={`/api/tenants/${id}/reports/statement.csv?month=${month}`}>Demonstrativo de {month} (CSV)</a>
          <a className="button ghost" href={`/api/tenants/${id}/reports/monthly.pdf?month=${month}`}>Relatório de {month} (PDF)</a>
        </div>
      </div>

      {rep && rep.daily.length > 1 && <div className="card"><h2>Custo por dia</h2><DailyChart points={rep.daily} /></div>}

      <div className="card">
        <h2>Resultado</h2>
        <Bars rows={rep?.rows ?? []} max={15} />
        {rep && rep.rows.length > 15 && (
          <div className="table-wrap" style={{ marginTop: 16 }}>
            <table>
              <thead><tr><th>Item</th><th style={{ textAlign: "right" }}>Custo</th></tr></thead>
              <tbody>
                {rep.rows.map((r) => (
                  <tr key={r.key + r.currency}><td>{r.label}</td><td className="num">{money(r.cost, r.currency)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
