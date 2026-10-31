"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import AlertList from "@/components/AlertList";
import Channels from "@/components/Channels";
import { api, can, categoryLabel, errorText, type Alert } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function Alertas() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [status, setStatus] = useState("active");
  const [category, setCategory] = useState("");
  const [items, setItems] = useState<Alert[]>([]);
  const [msg, setMsg] = useState("");

  const load = () => {
    const q = new URLSearchParams({ status });
    if (category) q.set("category", category);
    api<Alert[]>(`/tenants/${id}/alerts?${q}`).then(setItems);
  };
  useEffect(() => { load(); }, [id, status, category]);

  const canAct = can(me, id, "finops.manage") || can(me, id, "actions.execute") || can(me, id, "members.manage");
  const act = (what: string) => async (a: Alert) => {
    try {
      await api(`/tenants/${id}/alerts/${a.id}/${what}`, { method: "POST" });
      load();
    } catch (err) {
      setMsg(errorText(err));
    }
  };

  return (
    <div className="stack">
      <div className="card inline">
        <label>Situação
          <select id="al-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="active">Ativos</option><option value="open">Novos</option><option value="acknowledged">Reconhecidos</option><option value="resolved">Resolvidos</option><option value="all">Todos</option>
          </select>
        </label>
        <label>Tipo
          <select id="al-cat" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">Todos</option>
            {["cost", "health", "security"].map((c) => <option key={c} value={c}>{categoryLabel[c]}</option>)}
          </select>
        </label>
        {msg && <span className="error">{msg}</span>}
      </div>
      <div className="card"><AlertList items={items} onAck={canAct ? act("ack") : undefined} onResolve={canAct ? act("resolve") : undefined} /></div>
      {can(me, id, "members.manage") && <Channels base={`/tenants/${id}`} />}
    </div>
  );
}
