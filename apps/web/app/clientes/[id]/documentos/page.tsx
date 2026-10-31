"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, errorText, kindLabel, money, type Doc } from "@/lib/api";
import { useMe } from "@/lib/useMe";

function size(n: number) {
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`;
}

export default function Documentos() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [docs, setDocs] = useState<Doc[]>([]);
  const [year, setYear] = useState(String(new Date().getFullYear()));
  const [msg, setMsg] = useState("");

  const load = () => api<Doc[]>(`/tenants/${id}/documents?year=${year}`).then(setDocs).catch(() => setDocs([]));
  useEffect(() => { load(); }, [id, year]);

  async function upload(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setMsg("");
    const form = e.currentTarget;
    try {
      await api(`/tenants/${id}/documents`, { method: "POST", body: new FormData(form) });
      form.reset();
      setMsg("Documento publicado.");
      load();
    } catch (err) {
      setMsg(errorText(err, "Não foi possível publicar. Aceitos: PDF, XML da NF-e, ZIP ou CSV até 20 MB."));
    }
  }

  async function remove(d: Doc) {
    await api(`/tenants/${id}/documents/${d.id}`, { method: "DELETE" });
    load();
  }

  const years = Array.from({ length: 5 }, (_, i) => String(new Date().getFullYear() - i));

  return (
    <div className="stack">
      {me?.is_admin && (
        <form className="card inline" onSubmit={upload}>
          <label>Arquivo<input id="doc-file" name="file" type="file" required accept=".pdf,.xml,.zip,.csv" /></label>
          <label>Tipo
            <select id="doc-kind" name="kind" defaultValue="nota_fiscal">
              {Object.entries(kindLabel).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label>Competência<input id="doc-period" name="billing_period" type="month" required /></label>
          <label>Número<input id="doc-number" name="number" placeholder="NF 1234" /></label>
          <label>Valor<input id="doc-amount" name="amount" inputMode="decimal" placeholder="1500.00" /></label>
          <label>Moeda<input id="doc-currency" name="currency" defaultValue="BRL" size={4} /></label>
          <label>Vencimento<input id="doc-due" name="due_date" type="date" /></label>
          <button type="submit">Publicar para o cliente</button>
          {msg && <span className="muted">{msg}</span>}
        </form>
      )}

      <div className="card inline">
        <label>Ano
          <select id="doc-year" value={year} onChange={(e) => setYear(e.target.value)}>
            {years.map((y) => <option key={y}>{y}</option>)}
          </select>
        </label>
        <p className="muted">Cada download fica registrado na auditoria.</p>
      </div>

      <div className="card table-wrap">
        <table>
          <thead><tr><th>Competência</th><th>Tipo</th><th>Número</th><th>Valor</th><th>Vencimento</th><th>Arquivo</th><th /></tr></thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id}>
                <td className="mono">{d.billing_period.slice(0, 7)}</td>
                <td>{kindLabel[d.kind] ?? d.kind}</td>
                <td>{d.number ?? "—"}</td>
                <td className="num">{d.amount ? money(d.amount, d.currency ?? "BRL") : "—"}</td>
                <td>{d.due_date ? new Date(d.due_date + "T00:00").toLocaleDateString("pt-BR") : "—"}</td>
                <td><span className="muted">{d.filename} · {size(d.size_bytes)}</span></td>
                <td className="actions">
                  <a className="button" href={`/api/tenants/${id}/documents/${d.id}/download`}>Baixar</a>
                  {me?.is_admin && <button className="danger" onClick={() => remove(d)}>Excluir</button>}
                </td>
              </tr>
            ))}
            {docs.length === 0 && <tr><td colSpan={7} className="muted">Nenhum documento em {year}.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
