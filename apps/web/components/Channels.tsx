"use client";

import { useEffect, useState } from "react";
import { api, categoryLabel, errorText, severityLabel, type Channel } from "@/lib/api";

const TYPE_LABEL: Record<string, string> = { email: "E-mail", slack: "Slack", teams: "Microsoft Teams", webhook: "Webhook" };

/** Canais de notificação de uma empresa (base=/tenants/{id}) ou da plataforma (base=/admin). */
export default function Channels({ base, platform = false }: { base: string; platform?: boolean }) {
  const [items, setItems] = useState<Channel[]>([]);
  const [type, setType] = useState("email");
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [sev, setSev] = useState("warning");
  const [cats, setCats] = useState<string[]>(platform ? ["platform", "cost", "health", "security"] : ["cost", "health", "security"]);
  const [msg, setMsg] = useState("");
  const [secret, setSecret] = useState<string | null>(null);

  const load = () => api<Channel[]>(`${base}/channels`).then(setItems);
  useEffect(() => { load(); }, [base]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setMsg(""); setSecret(null);
    const body: Record<string, unknown> = { type, name, min_severity: sev, categories: cats };
    if (type === "email") body.emails = target.split(/[,;\s]+/).filter(Boolean);
    else body.url = target;
    try {
      const ch = await api<Channel>(`${base}/channels`, { method: "POST", body: JSON.stringify(body) });
      setName(""); setTarget("");
      if (ch.signing_secret) setSecret(ch.signing_secret);
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  async function test(ch: Channel) {
    await api(`${base}/channels/${ch.id}/test`, { method: "POST" });
    setMsg(`Teste enviado para ${ch.name}. O resultado aparece em alguns segundos.`);
    setTimeout(load, 5000);
  }

  const allCats = platform ? ["platform", "cost", "health", "security"] : ["cost", "health", "security"];

  return (
    <div className="card stack-sm">
      <h2>Onde avisar</h2>
      <form className="inline" onSubmit={add}>
        <label>Canal
          <select id="ch-type" value={type} onChange={(e) => setType(e.target.value)}>
            {Object.entries(TYPE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Nome<input id="ch-name" value={name} onChange={(e) => setName(e.target.value)} required placeholder="Financeiro" /></label>
        <label>{type === "email" ? "E-mails" : "URL do webhook"}
          <input id="ch-target" value={target} onChange={(e) => setTarget(e.target.value)} required size={36}
            placeholder={type === "email" ? "fin@empresa.com, ti@empresa.com" : type === "slack" ? "https://hooks.slack.com/services/…" : type === "teams" ? "https://….webhook.office.com/…" : "https://"} />
        </label>
        <label>A partir de
          <select id="ch-sev" value={sev} onChange={(e) => setSev(e.target.value)}>
            {Object.entries(severityLabel).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <fieldset className="cats">
          {allCats.map((c) => (
            <label key={c} className="check">
              <input type="checkbox" checked={cats.includes(c)} onChange={(e) => setCats(e.target.checked ? [...cats, c] : cats.filter((x) => x !== c))} /> {categoryLabel[c]}
            </label>
          ))}
        </fieldset>
        <button type="submit" disabled={!cats.length}>Adicionar</button>
      </form>
      {msg && <span className="note">{msg}</span>}
      {secret && (
        <p className="note">Segredo para validar a assinatura (<code>X-CloudView-Signature</code>), mostrado só agora: <code>{secret}</code></p>
      )}
      <p className="muted small">URLs de Slack, Teams e webhook ficam cifradas no cofre. Webhooks recebem o corpo em JSON assinado com HMAC-SHA256 sobre <code>timestamp.corpo</code>.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Canal</th><th>Destino</th><th>Avisa</th><th>Último envio</th><th /></tr></thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id}>
                <td><strong>{c.name}</strong><br /><span className="muted">{TYPE_LABEL[c.type]}</span></td>
                <td className="small">{c.target_hint}</td>
                <td className="small">{c.categories.map((x) => categoryLabel[x]).join(", ")}<br /><span className="muted">a partir de {severityLabel[c.min_severity]}</span></td>
                <td className="small">{c.last_delivery ? (c.last_delivery === "ok" ? <span className="pill ok">ok</span> : <span className="error">{c.last_delivery}</span>) : "—"}</td>
                <td className="actions">
                  <button className="ghost" onClick={() => test(c)}>Testar</button>
                  <button className="danger" onClick={async () => { await api(`${base}/channels/${c.id}`, { method: "DELETE" }); load(); }}>Remover</button>
                </td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={5} className="muted">Nenhum canal: os alertas só aparecem no painel.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
