"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, roleLabel, type Tenant } from "@/lib/api";
import { useMe } from "@/lib/useMe";

function slugify(s: string) {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function Clientes() {
  const me = useMe();
  const [items, setItems] = useState<Tenant[]>([]);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [err, setErr] = useState("");

  const load = () => api<Tenant[]>("/tenants").then(setItems);
  useEffect(() => { load(); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      await api("/tenants", { method: "POST", body: JSON.stringify({ name, slug: slug || slugify(name) }) });
      setName(""); setSlug("");
      load();
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 409 ? "Já existe um cliente com esse identificador." : "Não foi possível criar o cliente.");
    }
  }

  return (
    <div className="stack">
      <div>
        <h1>Clientes</h1>
        <p className="sub">Cada cliente agrupa suas contas AWS, Organizations, Azure e OCI.</p>
      </div>

      {me?.is_admin && (
        <form className="card inline" onSubmit={create}>
          <label>Nome<input id="tenant-name" value={name} onChange={(e) => setName(e.target.value)} required placeholder="Acme Ltda" /></label>
          <label>Identificador<input id="tenant-slug" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder={slugify(name) || "acme"} /></label>
          <button type="submit">Adicionar cliente</button>
          {err && <span className="error">{err}</span>}
        </form>
      )}

      <div className="card table-wrap">
        <table>
          <thead>
            <tr><th>Cliente</th><th>Identificador</th><th>Seu acesso</th><th>Criado em</th></tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.id}>
                <td><Link href={`/clientes/${t.id}`}>{t.name}</Link></td>
                <td className="mono">{t.slug}</td>
                <td>{me?.is_admin ? "Administrador" : roleLabel[me?.tenants[t.id] ?? ""]}</td>
                <td>{new Date(t.created_at).toLocaleDateString("pt-BR")}</td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr><td colSpan={4} className="muted">Nenhum cliente ainda.{me?.is_admin ? " Adicione o primeiro acima." : ""}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
