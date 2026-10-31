"use client";

import { useEffect, useState } from "react";
import { api, type AuditEntry } from "@/lib/api";

export default function Auditoria() {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [action, setAction] = useState("");

  useEffect(() => {
    const q = action ? `?action=${encodeURIComponent(action)}` : "";
    api<AuditEntry[]>(`/audit${q}`).then(setItems).catch(() => setItems([]));
  }, [action]);

  return (
    <div className="stack">
      <div>
        <h1>Auditoria</h1>
        <p className="sub">Registro imutável de logins e alterações. Nada aqui pode ser editado ou apagado.</p>
      </div>
      <div className="card inline">
        <label>Filtrar por ação
          <select id="audit-action" value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">Todas</option>
            <option value="auth.">Login / logout</option>
            <option value="tenant.">Clientes e acessos</option>
          </select>
        </label>
      </div>
      <div className="card table-wrap">
        <table>
          <thead><tr><th>Quando</th><th>Usuário</th><th>Ação</th><th>Alvo</th><th>IP</th><th>Resultado</th></tr></thead>
          <tbody>
            {items.map((e) => (
              <tr key={e.id}>
                <td className="mono">{new Date(e.ts).toLocaleString("pt-BR")}</td>
                <td>{e.user_email ?? "—"}</td>
                <td className="mono">{e.action}</td>
                <td>{e.target ?? "—"}</td>
                <td className="mono">{e.ip ?? "—"}</td>
                <td><span className={`pill ${e.outcome === "success" ? "ok" : "bad"}`}>{e.outcome === "success" ? "ok" : "falha"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
