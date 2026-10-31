"use client";

import { Fragment, useEffect, useState } from "react";
import { api, errorText, type Assignment, type CloudAccount, type ImportResult, type Tenant } from "@/lib/api";

function AssignPanel({ account, tenants, onChange }: { account: CloudAccount; tenants: Tenant[]; onChange: () => void }) {
  const [rules, setRules] = useState<Assignment[]>([]);
  const [tenant, setTenant] = useState(tenants[0]?.id ?? "");
  const [byTag, setByTag] = useState(false);
  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [from, setFrom] = useState("");
  const [msg, setMsg] = useState("");
  const name = (id: string) => tenants.find((t) => t.id === id)?.name ?? id;

  const load = () => api<Assignment[]>(`/admin/accounts/${account.id}/assignments`).then(setRules);
  useEffect(() => { load(); }, [account.id]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      await api(`/admin/accounts/${account.id}/assignments`, {
        method: "POST",
        body: JSON.stringify({ tenant_id: tenant, tag_key: byTag ? tagKey : null, tag_value: byTag ? tagValue : null, valid_from: from || null }),
      });
      load(); onChange();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <div className="stack-sm assign">
      {rules.map((r) => (
        <div key={r.id} className="rule">
          <span><strong>{name(r.tenant_id)}</strong> · {r.tag_key ? `tag ${r.tag_key} = ${r.tag_value}` : "conta inteira"}{r.valid_from ? ` · desde ${r.valid_from}` : ""}{r.valid_to ? ` até ${r.valid_to}` : ""}</span>
          <button className="danger" onClick={async () => { await api(`/admin/assignments/${r.id}`, { method: "DELETE" }); load(); onChange(); }}>Remover</button>
        </div>
      ))}
      <form className="inline" onSubmit={add}>
        <select aria-label="Cliente" value={tenant} onChange={(e) => setTenant(e.target.value)}>
          {tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <label className="check"><input type="checkbox" checked={byTag} onChange={(e) => setByTag(e.target.checked)} /> só linhas com a tag</label>
        {byTag && <>
          <input aria-label="Chave da tag" placeholder="cost-center" value={tagKey} onChange={(e) => setTagKey(e.target.value)} required />
          <input aria-label="Valor da tag" placeholder="empresa-a" value={tagValue} onChange={(e) => setTagValue(e.target.value)} required />
        </>}
        <input aria-label="Válido desde" type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        <button type="submit">Atribuir</button>
        {msg && <span className="error">{msg}</span>}
      </form>
    </div>
  );
}

export default function ContasNuvem() {
  const [accounts, setAccounts] = useState<CloudAccount[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [onlyUnassigned, setOnlyUnassigned] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [imports, setImports] = useState<ImportResult[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    api<CloudAccount[]>(`/admin/accounts${onlyUnassigned ? "?unassigned=true" : ""}`).then(setAccounts);
    api<ImportResult[]>("/admin/imports").then(setImports);
  };
  useEffect(() => { load(); }, [onlyUnassigned]);
  useEffect(() => { api<Tenant[]>("/tenants").then(setTenants); }, []);

  async function upload(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    setBusy(true); setMsg("");
    try {
      const r = await api<ImportResult>("/admin/imports/focus", { method: "POST", body: new FormData(form) });
      setMsg(`${r.source_rows} linhas lidas, ${r.stored_rows} gravadas${r.replaced_rows ? `, ${r.replaced_rows} substituídas` : ""}.` +
        (r.unassigned_accounts.length ? ` Contas sem cliente: ${r.unassigned_accounts.join(", ")}.` : ""));
      form.reset();
      load();
    } catch (err) {
      setMsg(errorText(err, "Arquivo inválido. Use o export FOCUS (CSV ou CSV.GZ)."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div>
        <h1>Contas de nuvem</h1>
        <p className="sub">Diga a qual empresa pertence cada conta. Uma organização ou conta pagadora pode ter contas de várias empresas; cada uma só vê as suas.</p>
      </div>

      <form className="card inline" onSubmit={upload}>
        <label>Arquivo de custos FOCUS<input id="imp-file" name="file" type="file" accept=".csv,.gz" required /></label>
        <label>Nuvem
          <select id="imp-provider" name="provider" defaultValue="">
            <option value="">Detectar</option><option value="aws">AWS</option><option value="azure">Azure</option><option value="oci">OCI</option>
          </select>
        </label>
        <button type="submit" disabled={busy}>{busy ? "Importando…" : "Importar"}</button>
        {msg && <span className="note">{msg}</span>}
      </form>
      <p className="muted">AWS: Billing → Data Exports → FOCUS 1.0. Azure: Cost Management → Exports → FOCUS. OCI: Billing → Cost and Usage Reports (FOCUS). Reimportar o mesmo período substitui os dados.</p>

      <div className="card inline">
        <label className="check"><input id="only-unassigned" type="checkbox" checked={onlyUnassigned} onChange={(e) => setOnlyUnassigned(e.target.checked)} /> Mostrar só contas sem empresa</label>
      </div>

      <div className="card table-wrap">
        <table>
          <thead><tr><th>Nuvem</th><th>Conta</th><th>Organização / pagadora</th><th>Empresa</th><th /></tr></thead>
          <tbody>
            {accounts.map((a) => (
              <Fragment key={a.id}>
                <tr>
                  <td className="mono">{a.provider.toUpperCase()}</td>
                  <td><span className="mono">{a.native_id}</span><br /><span className="muted">{a.name ?? ""}</span></td>
                  <td>{a.connection_name ?? "—"}<br /><span className="muted mono">{a.connection_native_id}</span></td>
                  <td>{a.tenants.length ? a.tenants.join(", ") : <span className="pill bad">sem empresa</span>}</td>
                  <td className="actions"><button className="ghost" onClick={() => setOpen(open === a.id ? null : a.id)}>{open === a.id ? "Fechar" : "Atribuir"}</button></td>
                </tr>
                {open === a.id && (
                  <tr><td colSpan={5}><AssignPanel account={a} tenants={tenants} onChange={load} /></td></tr>
                )}
              </Fragment>
            ))}
            {accounts.length === 0 && <tr><td colSpan={5} className="muted">Nenhuma conta. Importe um arquivo de custos para começar.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card table-wrap">
        <h2>Importações recentes</h2>
        <table>
          <thead><tr><th>Quando</th><th>Arquivo</th><th>Período</th><th>Linhas</th><th>Por</th></tr></thead>
          <tbody>
            {imports.map((i) => (
              <tr key={i.id}>
                <td>{new Date(i.created_at).toLocaleString("pt-BR")}</td>
                <td>{i.filename}</td>
                <td className="mono">{i.period_start} → {i.period_end}</td>
                <td className="num">{i.stored_rows}</td>
                <td>{i.created_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
