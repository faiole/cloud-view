"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, money, roleLabel, type CloudAccount, type CostSummary, type Tenant } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function Home() {
  const me = useMe();
  const router = useRouter();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [unassigned, setUnassigned] = useState<number | null>(null);
  const [mtd, setMtd] = useState<Record<string, string>>({});

  useEffect(() => {
    api<Tenant[]>("/tenants").then(async (ts) => {
      // Quem tem acesso a uma única empresa vai direto para ela
      if (!me?.is_admin && ts.length === 1) return router.replace(`/clientes/${ts[0].id}`);
      setTenants(ts);
      const entries = await Promise.all(
        ts.slice(0, 50).map(async (t) => {
          try {
            const s = await api<CostSummary>(`/tenants/${t.id}/costs/summary`);
            return [t.id, s.month_to_date.map((x) => money(x.cost, x.currency)).join(" + ") || "—"] as const;
          } catch {
            return [t.id, "—"] as const;
          }
        }),
      );
      setMtd(Object.fromEntries(entries));
    });
    if (me?.is_admin) api<CloudAccount[]>("/admin/accounts?unassigned=true").then((a) => setUnassigned(a.length));
  }, [me]);

  return (
    <div className="stack">
      <div>
        <h1>Visão geral</h1>
        <p className="sub">Olá, {me?.name || me?.email}.</p>
      </div>
      {me?.is_admin && (
        <div className="grid">
          <div className="card kpi"><div className="label">Empresas</div><div className="value">{tenants.length}</div></div>
          <div className="card kpi">
            <div className="label">Contas sem empresa</div>
            <div className="value">{unassigned ?? "—"} {unassigned ? <Link href="/contas" className="small">atribuir →</Link> : null}</div>
          </div>
        </div>
      )}
      <div className="card table-wrap">
        <table>
          <thead><tr><th>Empresa</th><th>Seu acesso</th><th style={{ textAlign: "right" }}>Mês atual</th></tr></thead>
          <tbody>
            {tenants.map((t) => (
              <tr key={t.id}>
                <td><Link href={`/clientes/${t.id}`}>{t.name}</Link></td>
                <td>{me?.is_admin ? "Administrador da plataforma" : roleLabel[me?.tenants[t.id] ?? "viewer"]}</td>
                <td className="num">{mtd[t.id] ?? "…"}</td>
              </tr>
            ))}
            {tenants.length === 0 && <tr><td colSpan={3} className="muted">Nenhuma empresa ainda.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
