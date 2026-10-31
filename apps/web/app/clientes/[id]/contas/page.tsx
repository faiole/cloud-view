"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type TenantAccount } from "@/lib/api";

export default function Contas() {
  const { id } = useParams<{ id: string }>();
  const [items, setItems] = useState<TenantAccount[]>([]);
  useEffect(() => { api<TenantAccount[]>(`/tenants/${id}/accounts`).then(setItems); }, [id]);

  return (
    <div className="card table-wrap">
      <p className="muted" style={{ marginBottom: 12 }}>
        Contas cujos custos pertencem a esta empresa, independentemente da organização ou conta pagadora onde estão.
      </p>
      <table>
        <thead><tr><th>Nuvem</th><th>Conta</th><th>Nome</th><th>O que entra</th></tr></thead>
        <tbody>
          {items.map((a) => (
            <tr key={a.id}>
              <td className="mono">{a.provider.toUpperCase()}</td>
              <td className="mono">{a.native_id}</td>
              <td>{a.name ?? "—"} {a.shared && <span className="pill todo">compartilhada</span>}</td>
              <td>{a.rules.join("; ")}</td>
            </tr>
          ))}
          {items.length === 0 && <tr><td colSpan={4} className="muted">Nenhuma conta atribuída ainda.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
