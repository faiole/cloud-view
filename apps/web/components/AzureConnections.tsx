"use client";

import { useState } from "react";
import { api, errorText, type AzureHubStatus, type ConnectionDetail } from "@/lib/api";

export function AzureHub({ hub, reload }: { hub: AzureHubStatus | null; reload: () => void }) {
  const [clientId, setClientId] = useState("");
  const [home, setHome] = useState("");
  const [mode, setMode] = useState<"certificate" | "secret">("certificate");
  const [cert, setCert] = useState("");
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [msg, setMsg] = useState("");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    const body: Record<string, string> = { client_id: clientId, home_tenant_id: home };
    if (mode === "certificate") { body.certificate_pem = cert; body.private_key_pem = key; } else body.client_secret = secret;
    try {
      await api("/admin/azure/hub", { method: "PUT", body: JSON.stringify(body) });
      setCert(""); setKey(""); setSecret(""); setMsg("App guardado no cofre.");
      reload();
    } catch (err) {
      setMsg(errorText(err, "Confira o client id e o tenant (formato GUID) e a credencial."));
    }
  }

  return (
    <div className="card stack-sm">
      <h2>App hub do Azure</h2>
      <p className="muted">
        App <strong>multi-tenant</strong> registrado no seu Entra ID. Cada cliente só dá consentimento e atribui as roles; nenhum segredo por cliente.
        Use certificado (recomendado) ou client secret; a credencial vai cifrada para o cofre.
      </p>
      <p>Situação: <strong>{hub?.configured ? `configurado (${hub.auth === "certificate" ? "certificado" : "secret"})` : "não configurado"}</strong>
        {hub?.client_id && <> · client id <span className="mono">{hub.client_id}</span></>}</p>
      <form className="stack-sm" onSubmit={save}>
        <div className="inline">
          <label>Client id do app<input id="az-cid" value={clientId} onChange={(e) => setClientId(e.target.value)} required placeholder="00000000-0000-…" size={38} /></label>
          <label>Tenant do app<input id="az-home" value={home} onChange={(e) => setHome(e.target.value)} required placeholder="tenant da sua empresa" size={38} /></label>
          <label>Credencial
            <select id="az-mode" value={mode} onChange={(e) => setMode(e.target.value as "certificate" | "secret")}>
              <option value="certificate">Certificado</option><option value="secret">Client secret</option>
            </select>
          </label>
        </div>
        {mode === "certificate" ? (
          <div className="inline">
            <label className="grow">Certificado (.crt, PEM)<textarea id="az-cert" rows={4} value={cert} onChange={(e) => setCert(e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" /></label>
            <label className="grow">Chave privada (.key, PEM)<textarea id="az-key" rows={4} value={key} onChange={(e) => setKey(e.target.value)} placeholder="-----BEGIN PRIVATE KEY-----" autoComplete="off" /></label>
          </div>
        ) : (
          <label>Client secret<input id="az-secret" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" size={44} /></label>
        )}
        <div className="inline"><button type="submit">Guardar app</button>{msg && <span className="note">{msg}</span>}</div>
      </form>
    </div>
  );
}

export function NewAzureConnection({ onCreated }: { onCreated: (c: ConnectionDetail) => void }) {
  const [form, setForm] = useState({ name: "", tenant_id: "", cost_source: "azure_cost_management", focus_uri: "", billing_account_id: "" });
  const [msg, setMsg] = useState("");

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      const c = await api<ConnectionDetail>("/admin/connections/azure", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          tenant_id: form.tenant_id.trim(),
          cost_source: form.cost_source,
          focus_uri: form.cost_source === "focus_blob" ? form.focus_uri : null,
          billing_account_id: form.billing_account_id || null,
        }),
      });
      setForm({ name: "", tenant_id: "", cost_source: "azure_cost_management", focus_uri: "", billing_account_id: "" });
      onCreated(c);
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <form className="card inline" onSubmit={create}>
      <label>Nome<input id="az-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required placeholder="Cliente X (Azure)" /></label>
      <label>Tenant do cliente<input id="az-tenant" value={form.tenant_id} onChange={(e) => setForm({ ...form, tenant_id: e.target.value })} required placeholder="ID do diretório (GUID)" size={38} /></label>
      <label>Custos
        <select id="az-cost" value={form.cost_source} onChange={(e) => setForm({ ...form, cost_source: e.target.value })}>
          <option value="azure_cost_management">Cost Management (sem tags)</option>
          <option value="focus_blob">Export FOCUS no Blob (com tags)</option>
          <option value="manual">Só importação manual</option>
        </select>
      </label>
      {form.cost_source === "focus_blob" && (
        <label>Local do export<input id="az-blob" value={form.focus_uri} onChange={(e) => setForm({ ...form, focus_uri: e.target.value })} required placeholder="https://conta.blob.core.windows.net/container/pasta" size={44} /></label>
      )}
      <label>Conta de cobrança (opcional)<input id="az-ba" value={form.billing_account_id} onChange={(e) => setForm({ ...form, billing_account_id: e.target.value })} placeholder="para invoices" /></label>
      <button type="submit">Adicionar conexão Azure</button>
      {msg && <span className="error">{msg}</span>}
    </form>
  );
}

export function AzureOnboarding({ c, hub }: { c: ConnectionDetail; hub: AzureHubStatus | null }) {
  const app = c.client_id || hub?.client_id || "<client-id-do-app>";
  const consent = `https://login.microsoftonline.com/${c.native_id}/adminconsent?client_id=${app}`;
  return (
    <div className="stack-sm onboarding">
      <p>1. Um administrador global do tenant <span className="mono">{c.native_id}</span> abre o link e aceita o consentimento:</p>
      <pre className="code">{consent}</pre>
      <p>2. No Cloud Shell do cliente, atribua as roles (grupo de gerenciamento recomendado, cobre subscriptions novas):</p>
      <pre className="code">{`./onboarding/azure/grant-access.sh ${app} /providers/Microsoft.Management/managementGroups/<id-do-grupo>`}</pre>
      <p className="muted">
        Opções: <code>--actions</code> cria o papel <em>Cloud View Operator</em> (reiniciar/ligar/desligar VMs; atribua só nos grupos de recursos desejados e marque as VMs com <code>cloudview-actions=enabled</code>).
        <code> --export-storage &lt;id&gt;</code> libera a leitura do export FOCUS.
      </p>
      <p>3. Clique em <strong>Testar</strong>: as subscriptions aparecem em Contas de nuvem para atribuir às empresas.</p>
    </div>
  );
}
