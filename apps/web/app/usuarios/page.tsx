"use client";

import { useEffect, useState } from "react";
import { api, errorText, roleLabel, type AdminUser } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function Usuarios() {
  const me = useMe();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [q, setQ] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [admin, setAdmin] = useState(false);
  const [msg, setMsg] = useState("");

  const load = () => api<AdminUser[]>(`/admin/users${q ? `?q=${encodeURIComponent(q)}` : ""}`).then(setUsers);
  useEffect(() => { load(); }, [q]);

  async function act(u: AdminUser, path: string, label: string, body?: object) {
    setMsg("");
    try {
      await api(`/admin/users/${u.id}/${path}`, { method: body ? "PUT" : "POST", body: body ? JSON.stringify(body) : undefined });
      setMsg(`${label}: ${u.email}`);
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    try {
      const u = await api<AdminUser>("/admin/users", { method: "POST", body: JSON.stringify({ email, name: name || null, platform_admin: admin }) });
      setMsg(u.invite_email_sent === false ? "Usuário criado, mas o e-mail falhou." : `Convite enviado para ${u.email}.`);
      setEmail(""); setName(""); setAdmin(false);
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <div className="stack">
      <div>
        <h1>Usuários</h1>
        <p className="sub">Todas as pessoas com acesso ao painel. O acesso a cada empresa é dado na aba Acessos do cliente.</p>
      </div>

      <form className="card inline" onSubmit={invite}>
        <label>E-mail<input id="u-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Nome<input id="u-name" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="check"><input id="u-admin" type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} /> Administrador da plataforma</label>
        <button type="submit">Convidar</button>
        {msg && <span className="note">{msg}</span>}
      </form>

      <div className="card inline">
        <label>Buscar<input id="u-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="nome ou e-mail" /></label>
      </div>

      <div className="card table-wrap">
        <table>
          <thead><tr><th>Pessoa</th><th>Empresas e papéis</th><th>Último acesso</th><th>Situação</th><th /></tr></thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>{u.name ? <><strong>{u.name}</strong><br /></> : null}<span className="muted">{u.email}</span></td>
                <td>{u.memberships.length ? u.memberships.map((m) => <div key={m.tenant_id}>{m.tenant_name} · <span className="muted">{roleLabel[m.role]}</span></div>) : <span className="muted">—</span>}</td>
                <td>{u.last_login_at ? new Date(u.last_login_at).toLocaleString("pt-BR") : "nunca"}</td>
                <td>{u.disabled ? <span className="pill bad">desativado</span> : u.pending ? <span className="pill todo">convite pendente</span> : <span className="pill ok">ativo</span>}</td>
                <td className="actions">
                  {u.id !== me?.id && (
                    <>
                      {u.pending && <button className="ghost" onClick={() => act(u, "resend-invite", "Convite reenviado")}>Reenviar convite</button>}
                      <button className="ghost" onClick={() => act(u, "reset-mfa", "MFA redefinido")}>Redefinir MFA</button>
                      <button className="ghost" onClick={() => act(u, "logout", "Sessões encerradas")}>Encerrar sessões</button>
                      <button className="ghost" onClick={() => act(u, "platform-admin", "Papel alterado", { value: true })}>Tornar admin</button>
                      {u.disabled
                        ? <button onClick={() => act(u, "enable", "Reativado")}>Reativar</button>
                        : <button className="danger" onClick={() => act(u, "disable", "Desativado")}>Desativar</button>}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
