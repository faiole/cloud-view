"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, errorText, roleHelp, roleLabel, type Member, type Role, type Tenant } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function Acessos() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const router = useRouter();
  const [t, setT] = useState<Tenant | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("finance");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => api<Member[]>(`/tenants/${id}/members`).then(setMembers);
  useEffect(() => {
    load();
    api<Tenant>(`/tenants/${id}`).then(setT);
  }, [id]);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      const m = await api<Member>(`/tenants/${id}/members`, { method: "PUT", body: JSON.stringify({ email, name: name || null, role }) });
      setMsg(
        m.invite_email_sent === true ? `Convite enviado para ${m.email}.`
        : m.invite_email_sent === false ? `Acesso criado, mas o e-mail de convite falhou. Reenvie em Usuários.`
        : `Acesso de ${m.email} atualizado.`
      );
      setEmail(""); setName("");
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  async function changeRole(m: Member, r: Role) {
    await api(`/tenants/${id}/members`, { method: "PUT", body: JSON.stringify({ email: m.email, role: r }) });
    load();
  }

  async function remove(m: Member) {
    try {
      await api(`/tenants/${id}/members/${m.user_id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  }

  return (
    <div className="stack">
      <form className="card inline" onSubmit={invite}>
        <label>E-mail<input id="m-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Nome<input id="m-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="opcional" /></label>
        <label>Papel
          <select id="m-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {(Object.keys(roleLabel) as Role[]).map((r) => <option key={r} value={r}>{roleLabel[r]}</option>)}
          </select>
        </label>
        <button type="submit">Conceder acesso</button>
        <span className="muted">{roleHelp[role]}</span>
        {msg && <span className="note">{msg}</span>}
      </form>
      <p className="muted">Quem ainda não tem conta recebe um e-mail para criar a senha e cadastrar o MFA. Alterar acessos pede o código MFA de novo se o último foi há mais de 5 minutos.</p>

      <div className="card table-wrap">
        <table>
          <thead><tr><th>Pessoa</th><th>Papel</th><th>Situação</th><th /></tr></thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.user_id}>
                <td>{m.name ? <><strong>{m.name}</strong><br /></> : null}<span className="muted">{m.email}</span></td>
                <td>
                  <select aria-label={`Papel de ${m.email}`} value={m.role} onChange={(e) => changeRole(m, e.target.value as Role)} disabled={m.user_id === me?.id}>
                    {(Object.keys(roleLabel) as Role[]).map((r) => <option key={r} value={r}>{roleLabel[r]}</option>)}
                  </select>
                </td>
                <td>{m.disabled ? <span className="pill bad">desativado</span> : m.pending ? <span className="pill todo">convite pendente</span> : <span className="pill ok">ativo</span>}</td>
                <td className="actions">{m.user_id !== me?.id && <button className="danger" onClick={() => remove(m)}>Remover</button>}</td>
              </tr>
            ))}
            {members.length === 0 && <tr><td colSpan={4} className="muted">Ninguém além dos administradores da plataforma.</td></tr>}
          </tbody>
        </table>
      </div>

      {me?.is_admin && t && (
        <div className="card stack">
          <h2>Excluir cliente</h2>
          <p className="muted">Digite <code>{t.slug}</code> para confirmar. Os custos voltam a ficar sem dono e a exclusão fica na auditoria.</p>
          <form className="inline" onSubmit={async (e) => { e.preventDefault(); await api(`/tenants/${id}`, { method: "DELETE" }); router.push("/clientes"); }}>
            <input id="confirm-slug" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Confirmação" />
            <button className="danger" type="submit" disabled={confirm !== t.slug}>Excluir</button>
          </form>
        </div>
      )}
    </div>
  );
}
