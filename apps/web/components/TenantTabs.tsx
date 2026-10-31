"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, can, type Perm, type Tenant } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const TABS: { href: string; label: string; perm: Perm | null }[] = [
  { href: "", label: "Custos", perm: "costs.read" },
  { href: "/relatorios", label: "Relatórios", perm: "reports.read" },
  { href: "/otimizacao", label: "Otimização", perm: "costs.read" },
  { href: "/orcamentos", label: "Orçamentos", perm: "costs.read" },
  { href: "/alertas", label: "Alertas", perm: null },
  { href: "/recursos", label: "Recursos", perm: "resources.read" },
  { href: "/saude", label: "Saúde", perm: "resources.read" },
  { href: "/rede", label: "Rede", perm: "resources.read" },
  { href: "/documentos", label: "Invoices e notas", perm: "invoices.read" },
  { href: "/contas", label: "Contas", perm: "costs.read" },
  { href: "/acessos", label: "Acessos", perm: "members.manage" },
];

export default function TenantTabs({ id, children }: { id: string; children: React.ReactNode }) {
  const me = useMe();
  const path = usePathname();
  const [t, setT] = useState<Tenant | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    api<Tenant>(`/tenants/${id}`).then(setT).catch(() => setMissing(true));
  }, [id]);

  if (missing) return <p className="muted">Cliente não encontrado.</p>;
  const base = `/clientes/${id}`;
  return (
    <div className="stack">
      <div>
        <h1>{t?.name ?? "…"}</h1>
        <p className="sub mono">{t?.slug}</p>
      </div>
      <nav className="tabs" aria-label="Seções do cliente">
        {TABS.filter((tab) => !tab.perm || can(me, id, tab.perm)).map((tab) => {
          const href = base + tab.href;
          return (
            <Link key={tab.href} href={href} className={path === href ? "active" : ""}>
              {tab.label}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
