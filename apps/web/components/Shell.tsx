"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Me } from "@/lib/api";
import { MeContext } from "@/lib/useMe";

const NAV = [
  { href: "/", label: "Visão geral" },
  { href: "/clientes", label: "Empresas" },
  { href: "/contas", label: "Contas de nuvem", admin: true },
  { href: "/usuarios", label: "Usuários", admin: true },
  { href: "/auditoria", label: "Auditoria", admin: true },
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const path = usePathname();

  useEffect(() => {
    api<Me>("/auth/me").then(setMe).catch(() => {});
  }, []);

  async function logout() {
    const r = await api<{ logout_url: string }>("/auth/logout", { method: "POST" });
    window.location.href = r.logout_url;
  }

  if (!me) return <div className="splash">Verificando sessão…</div>;

  return (
    <MeContext.Provider value={me}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark" aria-hidden>◩</span> Cloud View
          </div>
          <nav>
            {NAV.filter((n) => !n.admin || me.is_admin).map((n) => {
              const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
              return (
                <Link key={n.href} href={n.href} className={active ? "active" : ""}>
                  {n.label}
                </Link>
              );
            })}
          </nav>
          <div className="sidebar-foot">
            <div className="who">
              <strong>{me.name || me.email}</strong>
              <span>{me.is_admin ? "Administrador" : "Usuário"}</span>
            </div>
            <button className="ghost" onClick={logout}>Sair</button>
          </div>
        </aside>
        <main className="content">{children}</main>
      </div>
    </MeContext.Provider>
  );
}
