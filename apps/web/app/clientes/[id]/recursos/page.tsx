"use client";

import { useParams } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import Findings from "@/components/Findings";
import { actionLabel, ago, api, can, errorText, INSTANCE_TYPES, PROVIDER_LABEL, typeLabel, type ActionReq, type Resource } from "@/lib/api";
import { useMe } from "@/lib/useMe";

function healthPill(r: Resource) {
  if (!INSTANCE_TYPES.includes(r.type)) return null;
  const h = r.health ?? "unknown";
  const cls = h === "ok" ? "ok" : h === "impaired" ? "bad" : "todo";
  const label = { ok: "saudável", impaired: "com falha", initializing: "iniciando", unknown: "—" }[h] ?? h;
  return <span className={`pill ${cls}`}>{label}</span>;
}

function ActionPanel({ tenantId, r, onDone }: { tenantId: string; r: Resource; onDone: () => void }) {
  const stopped = r.state === "stopped" || r.state === "deallocated";
  const [action, setAction] = useState(stopped ? "start" : "reboot");
  const [confirm, setConfirm] = useState("");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState("");
  const expected = r.name || r.native_id;
  const enabled = r.tags["cloudview-actions"] === "enabled" || r.tags["cloudview.actions"] === "enabled";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      await api(`/tenants/${tenantId}/resources/${r.id}/actions`, {
        method: "POST",
        body: JSON.stringify({ action, confirm, reason: reason || null }),
      });
      setMsg("Pedido enviado. O resultado aparece em Ações recentes.");
      onDone();
    } catch (err) {
      setMsg(errorText(err, "Não foi possível pedir a ação."));
    }
  }

  if (!enabled) {
    return <p className="muted">Ações rápidas desabilitadas. Para habilitar, adicione a tag <code>cloudview-actions=enabled</code> na instância.</p>;
  }
  return (
    <form className="inline" onSubmit={submit}>
      <select aria-label="Ação" value={action} onChange={(e) => setAction(e.target.value)}>
        {r.state === "running" && <><option value="reboot">Reiniciar</option><option value="stop">{r.type === "vm" ? "Desligar (desalocar)" : "Desligar"}</option></>}
        {stopped && <option value="start">Ligar</option>}
      </select>
      <input aria-label="Motivo" placeholder="motivo (opcional)" value={reason} onChange={(e) => setReason(e.target.value)} />
      <input aria-label="Confirmação" placeholder={`digite ${expected}`} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      <button className="danger" type="submit" disabled={confirm !== expected}>{actionLabel[action]}</button>
      {msg && <span className="note">{msg}</span>}
    </form>
  );
}

export default function Recursos() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [type, setType] = useState("ec2_instance,vm,oci_instance");
  const [q, setQ] = useState("");
  const [onlyFindings, setOnlyFindings] = useState(false);
  const [items, setItems] = useState<Resource[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [actions, setActions] = useState<ActionReq[]>([]);

  const load = () => {
    const p = new URLSearchParams({ type });
    if (q) p.set("q", q);
    if (onlyFindings) p.set("with_findings", "true");
    api<Resource[]>(`/tenants/${id}/resources?${p}`).then(setItems);
    api<ActionReq[]>(`/tenants/${id}/actions`).then(setActions);
  };
  useEffect(() => { load(); }, [id, type, q, onlyFindings]);
  // Acompanha ações em andamento
  useEffect(() => {
    if (!actions.some((a) => a.status === "queued" || a.status === "running")) return;
    const t = setTimeout(load, 3000);
    return () => clearTimeout(t);
  }, [actions]);

  const canAct = can(me, id, "actions.execute");
  const isInstance = type.startsWith("ec2_instance");

  return (
    <div className="stack">
      <div className="card inline">
        <label>Tipo
          <select id="res-type" value={type} onChange={(e) => setType(e.target.value)}>
            {Object.entries(typeLabel).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Buscar<input id="res-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="nome, id, IP ou tipo" /></label>
        <label className="check"><input id="res-findings" type="checkbox" checked={onlyFindings} onChange={(e) => setOnlyFindings(e.target.checked)} /> Só com alertas ou dicas</label>
        <span className="muted">{items.length} itens · coletado {ago(items[0]?.collected_at ?? null)}</span>
      </div>

      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>Nome</th>
              {isInstance && <><th>Modelo</th><th>Estado</th><th>Saúde</th></>}
              <th>Rede</th>
              <th>Conta / região</th>
              <th>Alertas e dicas</th>
              {isInstance && canAct && <th />}
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <Fragment key={r.id}>
                <tr>
                  <td><strong>{r.name || "—"}</strong><br /><span className="muted mono small" title={r.native_id}>{r.provider === "azure" ? `${String(r.attributes.resource_group ?? "")}` : r.provider === "oci" ? `…${r.native_id.slice(-12)}` : r.native_id}</span></td>
                  {isInstance && (
                    <>
                      <td className="mono">{r.instance_type}<br /><span className="muted">{PROVIDER_LABEL[r.provider] ?? r.provider} · {r.vcpu ?? "?"} vCPU · {r.memory_mib ? (r.memory_mib / 1024).toFixed(1) : "?"} GiB · {r.platform}</span></td>
                      <td>{r.state}</td>
                      <td>{healthPill(r)}</td>
                    </>
                  )}
                  <td className="mono small">
                    {r.private_ips.join(", ")}{r.public_ips.length ? <><br />pública {r.public_ips.join(", ")}</> : null}
                    {r.vpc_id && <><br /><span className="muted">{r.vpc_id.split("/").pop()}{r.subnet_id ? ` / ${r.subnet_id.split("/").pop()}` : ""}</span></>}
                    {(r.type === "ebs_volume" || r.type === "disk" || r.type === "block_volume") && <span className="muted">{String(r.attributes.size_gib)} GiB {String(r.attributes.volume_type)}</span>}
                    {(r.type === "vpc" || r.type === "subnet") && <span className="muted">{String(r.attributes.cidr)}</span>}
                  </td>
                  <td className="mono small">{r.account_name || r.account_native_id}<br /><span className="muted">{r.region}</span></td>
                  <td><Findings items={r.findings} /></td>
                  {isInstance && canAct && (
                    <td className="actions"><button className="ghost" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? "Fechar" : "Ações"}</button></td>
                  )}
                </tr>
                {open === r.id && (
                  <tr><td colSpan={8}><ActionPanel tenantId={id} r={r} onDone={load} /></td></tr>
                )}
              </Fragment>
            ))}
            {items.length === 0 && <tr><td colSpan={8} className="muted">Nada coletado ainda para esta empresa.</td></tr>}
          </tbody>
        </table>
      </div>

      {actions.length > 0 && (
        <div className="card table-wrap">
          <h2>Ações recentes</h2>
          <table>
            <thead><tr><th>Quando</th><th>Recurso</th><th>Ação</th><th>Por</th><th>Resultado</th></tr></thead>
            <tbody>
              {actions.map((a) => (
                <tr key={a.id}>
                  <td>{new Date(a.requested_at).toLocaleString("pt-BR")}</td>
                  <td className="mono">{a.resource_native_id}</td>
                  <td>{actionLabel[a.action]}{a.reason ? <><br /><span className="muted">{a.reason}</span></> : null}</td>
                  <td>{a.requested_by}</td>
                  <td>
                    <span className={`pill ${a.status === "succeeded" ? "ok" : a.status === "failed" ? "bad" : "todo"}`}>
                      {{ queued: "na fila", running: "executando", succeeded: "feito", failed: "falhou" }[a.status] ?? a.status}
                    </span>
                    {a.result?.state && <span className="muted"> → {a.result.state}</span>}
                    {a.result?.error && <><br /><span className="error small">{a.result.error}</span></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
