"use client";

import { Fragment, useEffect, useState } from "react";
import { AzureHub, AzureOnboarding, NewAzureConnection } from "@/components/AzureConnections";
import { NewOciConnection, OciHub, OciOnboarding } from "@/components/OciConnections";
import { ago, api, errorText, type AzureHubStatus, type ConnectionDetail, type HubStatus, type Job, type OciHubStatus } from "@/lib/api";

const STATUS: Record<string, [string, string]> = {
  ok: ["ok", "funcionando"],
  pending: ["todo", "aguardando teste"],
  error: ["bad", "erro"],
  manual: ["todo", "só importação"],
};
const KIND_LABEL: Record<string, string> = { discover: "Descoberta", costs: "Custos", invoices: "Invoices", aws_health: "AWS Health", inventory: "Inventário", health: "Saúde", action: "Ação" };

function Hub({ hub, reload }: { hub: HubStatus | null; reload: () => void }) {
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [arn, setArn] = useState("");
  const [msg, setMsg] = useState("");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api("/admin/aws/hub", { method: "PUT", body: JSON.stringify({ access_key_id: key, secret_access_key: secret, principal_arn: arn }) });
      setKey(""); setSecret(""); setMsg("Chave guardada no cofre.");
      reload();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  async function savePrincipal(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api("/admin/aws/hub/principal", { method: "PUT", body: JSON.stringify({ principal_arn: arn }) });
      setMsg("Identidade salva.");
      reload();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <div className="card stack-sm">
      <h2>Identidade hub da AWS</h2>
      <p className="muted">
        É a identidade que assume as roles <code>CloudViewReadOnly</code> nas contas dos clientes.
        Se o servidor roda numa EC2, use a <em>instance profile</em> (modo padrão, sem chave).
        Em qualquer outro lugar, crie um usuário IAM com permissão apenas de <code>sts:AssumeRole</code> e cadastre a chave aqui; ela vai cifrada para o cofre e não pode ser lida de volta.
      </p>
      <p>
        Modo atual: <strong>{hub?.mode === "vault" ? `chave no cofre (${hub.access_key_hint})` : "padrão (instance profile / ambiente)"}</strong>
        {hub?.principal_arn && <> · <span className="mono small">{hub.principal_arn}</span></>}
      </p>
      <form className="inline" onSubmit={save}>
        <input aria-label="Access key id" placeholder="AKIA…" value={key} onChange={(e) => setKey(e.target.value)} />
        <input aria-label="Secret access key" type="password" placeholder="secret access key" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" />
        <input aria-label="ARN da identidade" placeholder="arn:aws:iam::111122223333:user/cloudview-hub" value={arn} onChange={(e) => setArn(e.target.value)} required size={44} />
        <button type="submit" disabled={!key || !secret}>Guardar chave</button>
        <button type="button" className="ghost" onClick={savePrincipal} disabled={!arn}>Só salvar o ARN (modo padrão)</button>
        {hub?.mode === "vault" && <button type="button" className="danger" onClick={async () => { await api("/admin/aws/hub", { method: "DELETE" }); reload(); }}>Remover chave</button>}
      </form>
      {msg && <span className="note">{msg}</span>}
    </div>
  );
}

function Onboarding({ c, principal }: { c: ConnectionDetail; principal: string | null }) {
  const p = principal || "<ARN da identidade hub>";
  const params = `CollectorPrincipalArn=${p} ActionsPrincipalArn=${p} ExternalId=${c.external_id} EnableQuickActions=false`;
  const single = `aws cloudformation deploy --stack-name cloudview-access --template-file onboarding/aws/cloudformation/cloudview-roles.yaml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides ${params}`;
  const stackset = `aws cloudformation create-stack-set --stack-set-name cloudview-access --template-body file://onboarding/aws/cloudformation/cloudview-roles.yaml --permission-model SERVICE_MANAGED --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false --capabilities CAPABILITY_NAMED_IAM --parameters ${params.split(" ").map((kv) => { const [k, v] = kv.split("="); return `ParameterKey=${k},ParameterValue=${v}`; }).join(" ")}`;
  return (
    <div className="stack-sm onboarding">
      <p><strong>ExternalId:</strong> <code>{c.external_id}</code></p>
      <p>1. Na conta <span className="mono">{c.native_id}</span>, crie as roles:</p>
      <pre className="code">{single}</pre>
      {c.kind === "aws_organization" && (
        <>
          <p>2. Para as contas-membro, um StackSet com implantação automática (contas novas entram sozinhas):</p>
          <pre className="code">{stackset}</pre>
          <p className="muted">Depois rode <code>aws cloudformation create-stack-instances --stack-set-name cloudview-access --deployment-targets OrganizationalUnitIds=&lt;ou-raiz&gt; --regions us-east-1</code>.</p>
        </>
      )}
      <p className="muted">Para custos com tags (dividir contas compartilhadas), crie um Data Export <em>FOCUS 1.0</em> em <em>CSV</em> e informe o bucket no parâmetro <code>FocusExportBucket</code>. Para habilitar ações rápidas, use <code>EnableQuickActions=true</code> e marque as instâncias com <code>cloudview-actions=enabled</code>.</p>
    </div>
  );
}

export default function Conexoes() {
  const [conns, setConns] = useState<ConnectionDetail[]>([]);
  const [hub, setHub] = useState<HubStatus | null>(null);
  const [azHub, setAzHub] = useState<AzureHubStatus | null>(null);
  const [ociHub, setOciHub] = useState<OciHubStatus | null>(null);
  const [cloud, setCloud] = useState<"aws" | "azure" | "oci">("aws");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState({ name: "", kind: "aws_organization", account_id: "", cost_source: "cost_explorer", focus_s3_uri: "", regions: "" });

  const load = () => {
    api<ConnectionDetail[]>("/admin/connections").then(setConns);
    api<HubStatus>("/admin/aws/hub").then(setHub);
    api<AzureHubStatus>("/admin/azure/hub").then(setAzHub);
    api<OciHubStatus>("/admin/oci/hub").then(setOciHub);
    api<Job[]>("/admin/jobs?limit=40").then(setJobs);
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!jobs.some((j) => j.status === "queued" || j.status === "running")) return;
    const t = setTimeout(load, 4000);
    return () => clearTimeout(t);
  }, [jobs]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      const c = await api<ConnectionDetail>("/admin/connections", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          kind: form.kind,
          account_id: form.account_id,
          cost_source: form.cost_source,
          focus_s3_uri: form.cost_source === "focus_s3" ? form.focus_s3_uri : null,
          regions: form.regions ? form.regions.split(",").map((r) => r.trim()).filter(Boolean) : null,
        }),
      });
      setOpen(c.id);
      setMsg("Conexão criada. Implante o CloudFormation abaixo e clique em Testar.");
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  async function run(c: ConnectionDetail, what: "test" | "sync") {
    try {
      await api(`/admin/connections/${c.id}/${what}`, { method: "POST" });
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <div className="stack">
      <div>
        <h1>Conexões</h1>
        <p className="sub">Contas AWS, Organizations e tenants Azure de onde o painel coleta custos, invoices, inventário, rede e saúde automaticamente.</p>
      </div>

      <nav className="tabs" aria-label="Nuvem">
        <a href="#aws" className={cloud === "aws" ? "active" : ""} onClick={(e) => { e.preventDefault(); setCloud("aws"); }}>AWS</a>
        <a href="#azure" className={cloud === "azure" ? "active" : ""} onClick={(e) => { e.preventDefault(); setCloud("azure"); }}>Azure</a>
        <a href="#oci" className={cloud === "oci" ? "active" : ""} onClick={(e) => { e.preventDefault(); setCloud("oci"); }}>OCI</a>
      </nav>

      {cloud === "aws" && <Hub hub={hub} reload={load} />}
      {cloud === "azure" && <AzureHub hub={azHub} reload={load} />}
      {cloud === "azure" && <NewAzureConnection onCreated={(c) => { setOpen(c.id); setMsg("Conexão criada. Siga os passos abaixo e clique em Testar."); load(); }} />}

      {cloud === "aws" && <form className="card inline" onSubmit={create}>
        <label>Nome<input id="c-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required placeholder="Organização AB" /></label>
        <label>Tipo
          <select id="c-kind" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            <option value="aws_organization">AWS Organization (conta management)</option>
            <option value="aws_account">Conta AWS avulsa</option>
          </select>
        </label>
        <label>ID da conta<input id="c-account" value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })} required pattern="\d{12}" placeholder="111122223333" /></label>
        <label>Custos
          <select id="c-cost" value={form.cost_source} onChange={(e) => setForm({ ...form, cost_source: e.target.value })}>
            <option value="cost_explorer">Cost Explorer (sem tags)</option>
            <option value="focus_s3">Data Export FOCUS no S3 (com tags)</option>
            <option value="manual">Só importação manual</option>
          </select>
        </label>
        {form.cost_source === "focus_s3" && (
          <label>Local do export<input id="c-s3" value={form.focus_s3_uri} onChange={(e) => setForm({ ...form, focus_s3_uri: e.target.value })} placeholder="s3://bucket/prefixo/" required /></label>
        )}
        <label>Regiões<input id="c-regions" value={form.regions} onChange={(e) => setForm({ ...form, regions: e.target.value })} placeholder="todas (ou sa-east-1, us-east-1)" /></label>
        <button type="submit">Adicionar conexão</button>
        {msg && <span className="note">{msg}</span>}
      </form>}
      {cloud === "oci" && <NewOciConnection onCreated={(c) => { setOpen(c.id); setMsg("Conexão criada. Siga os passos abaixo."); load(); }} />}
      {cloud === "oci" && <OciHub hub={ociHub} reload={load} />}
      {cloud !== "aws" && msg && <span className="note">{msg}</span>}

      <div className="card table-wrap">
        <table>
          <thead><tr><th>Conexão</th><th>Contas</th><th>Custos</th><th>Situação</th><th>Última coleta</th><th /></tr></thead>
          <tbody>
            {conns.map((c) => {
              const [cls, label] = STATUS[c.status] ?? ["todo", c.status];
              return (
                <Fragment key={c.id}>
                  <tr>
                    <td><strong>{c.name}</strong><br /><span className="muted mono">{{ aws_organization: "AWS Organization", aws_account: "Conta AWS", azure_tenant: "Tenant Azure", oci_tenancy: "Tenancy OCI" }[c.kind] ?? c.kind} {c.native_id}</span></td>
                    <td className="num">{c.accounts}</td>
                    <td>{{ cost_explorer: "Cost Explorer", focus_s3: "FOCUS (S3)", azure_cost_management: "Cost Management", focus_blob: "FOCUS (Blob)", oci_usage_api: "Usage API", focus_oci: "FOCUS (Oracle)", manual: "importação" }[c.cost_source] ?? c.cost_source}</td>
                    <td><span className={`pill ${cls}`}>{label}</span>{c.last_error && <><br /><span className="error small">{c.last_error}</span></>}</td>
                    <td>{ago(c.last_sync_at)}</td>
                    <td className="actions">
                      {(c.role_arn || (c.provider !== "aws" && c.status !== "manual")) && <>
                        <button className="ghost" onClick={() => setOpen(open === c.id ? null : c.id)}>{open === c.id ? "Fechar" : "Como conectar"}</button>
                        <button className="ghost" onClick={() => run(c, "test")}>Testar</button>
                        <button onClick={() => run(c, "sync")} disabled={c.status !== "ok"}>Sincronizar agora</button>
                      </>}
                    </td>
                  </tr>
                  {open === c.id && c.role_arn && <tr><td colSpan={6}><Onboarding c={c} principal={hub?.principal_arn ?? null} /></td></tr>}
                  {open === c.id && c.provider === "azure" && <tr><td colSpan={6}><AzureOnboarding c={c} hub={azHub} /></td></tr>}
                  {open === c.id && c.provider === "oci" && c.status !== "manual" && <tr><td colSpan={6}><OciOnboarding c={c} reload={load} /></td></tr>}
                </Fragment>
              );
            })}
            {conns.length === 0 && <tr><td colSpan={6} className="muted">Nenhuma conexão ainda.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card table-wrap">
        <h2>Coletas recentes</h2>
        <table>
          <thead><tr><th>Quando</th><th>Tipo</th><th>Situação</th><th>Detalhe</th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{new Date(j.created_at).toLocaleString("pt-BR")}</td>
                <td>{KIND_LABEL[j.kind] ?? j.kind}</td>
                <td><span className={`pill ${j.status === "done" ? "ok" : j.status === "error" ? "bad" : "todo"}`}>{{ queued: "na fila", running: "executando", done: "ok", error: "erro" }[j.status] ?? j.status}</span>{j.attempts > 1 && <span className="muted"> · {j.attempts}ª tentativa</span>}</td>
                <td className="small">{j.error ? <span className="error">{j.error}</span> : <span className="muted mono">{Object.keys(j.result).length ? JSON.stringify(j.result).slice(0, 160) : ""}</span>}</td>
              </tr>
            ))}
            {jobs.length === 0 && <tr><td colSpan={4} className="muted">Nenhuma coleta ainda.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
