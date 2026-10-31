"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, roleLabel, type Member, type Tenant } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function Cliente() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const router = useRouter();
  const [t, setT] = useState<Tenant | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Member["role"]>("viewer");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState("");

  const loadMembers = () => api<Member[]>(`/tenants/${id}/members`).then(setMembers);

  useEffect(() => {
    api<Tenant>(`/tenants/${id}`).then(setT).catch(() => setMsg("Cliente não encontrado."));
    if (me?.is_admin) loadMembers();
  }, [id, me?.is_admin]);

  async function addMember(e: React.FormEvent) {
    e.preventDefault();
    await api(`/tenants/${id}/members`, { method: "PUT", body: JSON.stringify({ email, role }) });
    setEmail("");
    loadMembers();
  }

  async function removeMember(userId: string) {
    await api(`/tenants/${id}/members/${userId}`, { method: "DELETE" });
    loadMembers();
  }

  async function removeTenant() {
    await api(`/tenants/${id}`, { method: "DELETE" });
    router.push("/clientes");
  }

  if (!t) return <p className="muted">{msg || "Carregando…"}</p>;

  return (
    <div className="stack">
      <div>
        <h1>{t.name}</h1>
        <p className="sub mono">{t.slug}</p>
      </div>

      <div className="card">
        <h2>Conexão AWS</h2>
        <p className="muted">
          Use este ExternalId na trust policy da role <code>CloudViewReadOnly</code> criada pelo template de
          onboarding (<code>onboarding/aws/cloudformation</code>). O cadastro das contas chega na Fase 1.
        </p>
        <p><code>{t.aws_external_id}</code></p>
      </div>

      {me?.is_admin && (
        <>
          <div className="card stack">
            <h2>Acessos</h2>
            <form className="inline" onSubmit={addMember}>
              <label>E-mail<input id="member-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
              <label>Papel
                <select id="member-role" value={role} onChange={(e) => setRole(e.target.value as Member["role"])}>
                  <option value="viewer">Leitura</option>
                  <option value="operator">Operador (ações rápidas)</option>
                  <option value="client">Portal do cliente</option>
                </select>
              </label>
              <button type="submit">Conceder acesso</button>
            </form>
            <p className="muted">Alterar acessos pede o código MFA de novo se o último foi há mais de 5 minutos.</p>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Usuário</th><th>Papel</th><th /></tr></thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.user_id}>
                      <td>{m.name ? `${m.name} · ` : ""}{m.email}</td>
                      <td>{roleLabel[m.role]}</td>
                      <td style={{ textAlign: "right" }}><button className="danger" onClick={() => removeMember(m.user_id)}>Remover</button></td>
                    </tr>
                  ))}
                  {members.length === 0 && <tr><td colSpan={3} className="muted">Ninguém além dos administradores.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>

          <div className="card stack">
            <h2>Excluir cliente</h2>
            <p className="muted">Digite <code>{t.slug}</code> para confirmar. A exclusão fica registrada na auditoria.</p>
            <form className="inline" onSubmit={(e) => { e.preventDefault(); removeTenant(); }}>
              <input id="confirm-slug" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Confirmação" />
              <button className="danger" type="submit" disabled={confirm !== t.slug}>Excluir</button>
            </form>
          </div>
        </>
      )}
    </div>
  );
}
