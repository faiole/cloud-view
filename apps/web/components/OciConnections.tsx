"use client";

import { useState } from "react";
import { api, errorText, type ConnectionDetail, type OciHubStatus } from "@/lib/api";

const POLICIES = `Allow group CloudViewReaders to inspect all-resources in tenancy
Allow group CloudViewReaders to read all-resources in tenancy
Allow group CloudViewReaders to read usage-report in tenancy
Allow group CloudViewReaders to read optimizer-api-family in tenancy
Allow group CloudViewReaders to read announcements in tenancy`;

export function OciHub({ hub, reload }: { hub: OciHubStatus | null; reload: () => void }) {
  const [tenancy, setTenancy] = useState("");
  const [user, setUser] = useState("");
  const [region, setRegion] = useState("sa-saopaulo-1");
  const [msg, setMsg] = useState("");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api("/admin/oci/hub", { method: "PUT", body: JSON.stringify({ tenancy_id: tenancy, user_id: user, home_region: region }) });
      setMsg("Chave gerada. Cole a chave pública abaixo no usuário da OCI.");
      reload();
    } catch (err) {
      setMsg(errorText(err, "Confira os OCIDs (ocid1.tenancy… e ocid1.user…) e a região."));
    }
  }

  return (
    <details className="card">
      <summary><strong>Identidade hub da OCI (opcional, cross-tenancy)</strong> · {hub?.configured ? `configurada (${hub.fingerprint})` : "não configurada"}</summary>
      <div className="stack-sm" style={{ marginTop: 12 }}>
        <p className="muted">Use só se preferir políticas <em>endorse/admit</em> entre tenancies. O modo recomendado é a chave própria por conexão.</p>
        <form className="inline" onSubmit={save}>
          <label>Tenancy da plataforma<input id="oh-ten" value={tenancy} onChange={(e) => setTenancy(e.target.value)} required size={40} placeholder="ocid1.tenancy.oc1.." /></label>
          <label>Usuário<input id="oh-user" value={user} onChange={(e) => setUser(e.target.value)} required size={40} placeholder="ocid1.user.oc1.." /></label>
          <label>Região home<input id="oh-reg" value={region} onChange={(e) => setRegion(e.target.value)} required /></label>
          <button type="submit">Gerar chave</button>
          {msg && <span className="note">{msg}</span>}
        </form>
        {hub?.public_key && <pre className="code">{hub.public_key}</pre>}
      </div>
    </details>
  );
}

export function NewOciConnection({ onCreated }: { onCreated: (c: ConnectionDetail) => void }) {
  const [form, setForm] = useState({ name: "", tenancy_id: "", home_region: "sa-saopaulo-1", regions: "", cost_source: "oci_usage_api", auth: "own_key" });
  const [msg, setMsg] = useState("");

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      const c = await api<ConnectionDetail>("/admin/connections/oci", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          tenancy_id: form.tenancy_id.trim(),
          home_region: form.home_region.trim(),
          regions: form.regions ? form.regions.split(",").map((r) => r.trim()).filter(Boolean) : null,
          cost_source: form.cost_source,
          auth: form.auth,
        }),
      });
      setForm({ ...form, name: "", tenancy_id: "" });
      onCreated(c);
    } catch (err) {
      setMsg(errorText(err, "Confira o OCID da tenancy (ocid1.tenancy…) e a região (ex.: sa-saopaulo-1)."));
    }
  }

  return (
    <form className="card inline" onSubmit={create}>
      <label>Nome<input id="oc-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required placeholder="Cliente X (OCI)" /></label>
      <label>Tenancy do cliente<input id="oc-ten" value={form.tenancy_id} onChange={(e) => setForm({ ...form, tenancy_id: e.target.value })} required size={40} placeholder="ocid1.tenancy.oc1.." /></label>
      <label>Região home<input id="oc-home" value={form.home_region} onChange={(e) => setForm({ ...form, home_region: e.target.value })} required /></label>
      <label>Regiões<input id="oc-regions" value={form.regions} onChange={(e) => setForm({ ...form, regions: e.target.value })} placeholder="todas as assinadas" /></label>
      <label>Custos
        <select id="oc-cost" value={form.cost_source} onChange={(e) => setForm({ ...form, cost_source: e.target.value })}>
          <option value="oci_usage_api">Usage API (sem tags)</option>
          <option value="focus_oci">FOCUS reports da Oracle (com tags)</option>
          <option value="manual">Só importação manual</option>
        </select>
      </label>
      <label>Acesso
        <select id="oc-auth" value={form.auth} onChange={(e) => setForm({ ...form, auth: e.target.value })}>
          <option value="own_key">Chave própria (recomendado)</option>
          <option value="hub">Identidade hub (cross-tenancy)</option>
        </select>
      </label>
      <button type="submit">Adicionar conexão OCI</button>
      {msg && <span className="error">{msg}</span>}
    </form>
  );
}

export function OciOnboarding({ c, reload }: { c: ConnectionDetail; reload: () => void }) {
  const [user, setUser] = useState(c.oci_user_id ?? "");
  const [msg, setMsg] = useState("");

  async function saveUser(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api(`/admin/connections/${c.id}`, { method: "PATCH", body: JSON.stringify({ oci_user_id: user.trim() }) });
      setMsg("Usuário salvo. Clique em Testar.");
      reload();
    } catch (err) {
      setMsg(errorText(err, "OCID de usuário inválido (ocid1.user…)."));
    }
  }

  async function rotate() {
    await api(`/admin/connections/${c.id}/oci-key`, { method: "POST" });
    setMsg("Nova chave gerada: adicione a chave pública abaixo ao usuário e remova a antiga depois da próxima coleta.");
    reload();
  }

  if (c.oci_auth === "hub") {
    return <p className="muted">Acesso pela identidade hub: crie as políticas <em>endorse/admit</em> descritas em <code>onboarding/oci/README.md</code> e clique em Testar.</p>;
  }
  return (
    <div className="stack-sm onboarding">
      <p>1. Na tenancy do cliente, crie o usuário <code>cloudview</code> (sem console) no grupo <code>CloudViewReaders</code> e, em <em>API Keys</em>, cole esta chave pública:</p>
      <pre className="code">{c.oci_public_key}</pre>
      <p className="muted small">Fingerprint esperado: <span className="mono">{c.oci_fingerprint}</span>. A chave privada está só no cofre.</p>
      <p>2. Crie as políticas na compartment raiz:</p>
      <pre className="code">{POLICIES}</pre>
      <p className="muted small">Custos com tags, ações rápidas e rotação de chave: veja <code>onboarding/oci/README.md</code>.</p>
      <form className="inline" onSubmit={saveUser}>
        <label>3. OCID do usuário<input aria-label="OCID do usuário" value={user} onChange={(e) => setUser(e.target.value)} required size={48} placeholder="ocid1.user.oc1.." /></label>
        <button type="submit">Salvar</button>
        <button type="button" className="ghost" onClick={rotate}>Gerar nova chave</button>
        {msg && <span className="note">{msg}</span>}
      </form>
    </div>
  );
}
