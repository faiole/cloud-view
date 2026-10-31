"use client";

import { ago, categoryLabel, severityLabel, type Alert } from "@/lib/api";

export default function AlertList({
  items,
  onAck,
  onResolve,
  showTenant = false,
}: {
  items: Alert[];
  onAck?: (a: Alert) => void;
  onResolve?: (a: Alert) => void;
  showTenant?: boolean;
}) {
  if (!items.length) return <p className="muted">Nenhum alerta.</p>;
  return (
    <ul className="alerts">
      {items.map((a) => (
        <li key={a.id} className={`alert sev-${a.severity} ${a.status !== "open" ? "dim" : ""}`}>
          <div className="alert-main">
            <div className="alert-title">
              <span className={`pill sev-pill-${a.severity}`}>{severityLabel[a.severity]}</span>
              <strong>{a.title}</strong>
            </div>
            <p>{a.message}</p>
            <span className="muted small">
              {showTenant ? `${a.tenant_name ?? "Plataforma"} · ` : ""}{categoryLabel[a.category] ?? a.category} · {ago(a.created_at)}
              {a.status !== "open" ? ` · ${a.status === "acknowledged" ? "reconhecido" : "resolvido"}${a.status_by ? ` por ${a.status_by}` : ""}` : ""}
            </span>
          </div>
          <div className="actions">
            {a.status === "open" && onAck && <button className="ghost" onClick={() => onAck(a)}>Reconhecer</button>}
            {a.status !== "resolved" && onResolve && <button className="ghost" onClick={() => onResolve(a)}>Resolver</button>}
          </div>
        </li>
      ))}
    </ul>
  );
}
