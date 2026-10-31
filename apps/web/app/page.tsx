"use client";

import { useEffect, useState } from "react";
import { api, type Tenant } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const ROADMAP = [
  { label: "Login com senha + MFA (TOTP) obrigatório", done: true },
  { label: "Step-up MFA em ações sensíveis", done: true },
  { label: "Clientes, papéis por cliente e isolamento (RLS)", done: true },
  { label: "Trilha de auditoria imutável", done: true },
  { label: "Infraestrutura AWS (Terraform)", done: true },
  { label: "Conexões AWS / Organizations (Fase 1)", done: false },
  { label: "Custos, inventário e saúde (Fase 1)", done: false },
];

export default function Home() {
  const me = useMe();
  const [tenants, setTenants] = useState<Tenant[] | null>(null);

  useEffect(() => {
    api<Tenant[]>("/tenants").then(setTenants).catch(() => setTenants([]));
  }, []);

  const mfaMin = me ? Math.round(me.mfa_age_seconds / 60) : 0;

  return (
    <div className="stack">
      <div>
        <h1>Visão geral</h1>
        <p className="sub">Olá, {me?.name || me?.email}. Os dados de nuvem chegam a partir da Fase 1.</p>
      </div>
      <div className="grid">
        <div className="card kpi">
          <div className="label">Clientes visíveis</div>
          <div className="value">{tenants ? tenants.length : "—"}</div>
        </div>
        <div className="card kpi">
          <div className="label">Último MFA</div>
          <div className="value">{mfaMin < 1 ? "agora" : `há ${mfaMin} min`}</div>
        </div>
        <div className="card kpi">
          <div className="label">Ações sensíveis</div>
          <div className="value">
            <span className={`pill ${me?.step_up_valid ? "ok" : "todo"}`}>
              {me?.step_up_valid ? "liberadas" : "pedirão MFA"}
            </span>
          </div>
        </div>
      </div>
      <div className="card">
        <h2>Andamento do projeto</h2>
        <table>
          <tbody>
            {ROADMAP.map((r) => (
              <tr key={r.label}>
                <td>{r.label}</td>
                <td style={{ width: 120 }}>
                  <span className={`pill ${r.done ? "ok" : "todo"}`}>{r.done ? "Fase 0 ✓" : "próxima"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
