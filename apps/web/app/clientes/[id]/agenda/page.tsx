"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, can, DAYS_PT, errorText, INSTANCE_TYPES, PROVIDER_LABEL, type Resource, type Schedule } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const EMPTY = { name: "", timezone: "America/Sao_Paulo", days: [0, 1, 2, 3, 4], start_time: "08:00", stop_time: "20:00", mode: "tag", selector_tag: "agenda=comercial", resource_ids: [] as string[], enabled: true };

export default function Agenda() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const canEdit = can(me, id, "actions.execute");
  const [items, setItems] = useState<Schedule[]>([]);
  const [instances, setInstances] = useState<Resource[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState<string | null>(null);
  const [msg, setMsg] = useState("");

  const load = () => api<Schedule[]>(`/tenants/${id}/schedules`).then(setItems);
  useEffect(() => {
    load();
    api<Resource[]>(`/tenants/${id}/resources?type=${INSTANCE_TYPES.join(",")}`).then((rs) =>
      setInstances(rs.filter((r) => r.tags["cloudview-actions"] === "enabled" || r.tags["cloudview.actions"] === "enabled")));
  }, [id]);

  function body(f = form) {
    return {
      name: f.name, timezone: f.timezone, days: f.days, enabled: f.enabled,
      start_time: f.start_time || null, stop_time: f.stop_time || null,
      selector_tag: f.mode === "tag" ? f.selector_tag : null,
      resource_ids: f.mode === "list" ? f.resource_ids : [],
    };
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      await api(`/tenants/${id}/schedules${editing ? `/${editing}` : ""}`, { method: editing ? "PUT" : "POST", body: JSON.stringify(body()) });
      setForm(EMPTY); setEditing(null);
      load();
    } catch (err) {
      setMsg(errorText(err, "Confira os horários (ligar antes de desligar), o fuso e a seleção de instâncias."));
    }
  }

  function edit(s: Schedule) {
    setEditing(s.id);
    setForm({ name: s.name, timezone: s.timezone, days: s.days, start_time: s.start_time ?? "", stop_time: s.stop_time ?? "",
      mode: s.selector_tag ? "tag" : "list", selector_tag: s.selector_tag ?? "", resource_ids: s.resource_ids, enabled: s.enabled });
  }

  async function toggle(s: Schedule) {
    await api(`/tenants/${id}/schedules/${s.id}`, { method: "PUT", body: JSON.stringify({
      name: s.name, timezone: s.timezone, days: s.days, start_time: s.start_time, stop_time: s.stop_time,
      selector_tag: s.selector_tag, resource_ids: s.resource_ids, enabled: !s.enabled }) });
    load();
  }

  return (
    <div className="stack">
      <p className="muted">
        Liga e desliga instâncias em horários fixos. Só valem instâncias com ações rápidas habilitadas (tag <code>cloudview-actions=enabled</code>).
        Cada horário dispara uma vez por dia e aparece em <em>Recursos → Ações recentes</em> e na auditoria.
      </p>

      {canEdit && (
        <form className="card stack-sm" onSubmit={save}>
          <div className="inline">
            <label>Nome<input id="s-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required placeholder="Horário comercial" /></label>
            <label>Ligar às<input id="s-start" type="time" value={form.start_time} onChange={(e) => setForm({ ...form, start_time: e.target.value })} /></label>
            <label>Desligar às<input id="s-stop" type="time" value={form.stop_time} onChange={(e) => setForm({ ...form, stop_time: e.target.value })} /></label>
            <label>Fuso<input id="s-tz" value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} size={20} /></label>
          </div>
          <fieldset className="cats">
            {DAYS_PT.map((d, i) => (
              <label key={d} className="check">
                <input type="checkbox" checked={form.days.includes(i)} onChange={(e) => setForm({ ...form, days: e.target.checked ? [...form.days, i].sort() : form.days.filter((x) => x !== i) })} /> {d}
              </label>
            ))}
          </fieldset>
          <div className="inline">
            <label>Quais instâncias
              <select id="s-mode" value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
                <option value="tag">Pela tag</option><option value="list">Escolher na lista</option>
              </select>
            </label>
            {form.mode === "tag" ? (
              <label>Tag<input id="s-tag" value={form.selector_tag} onChange={(e) => setForm({ ...form, selector_tag: e.target.value })} placeholder="agenda=comercial" /></label>
            ) : (
              <fieldset className="cats">
                {instances.map((r) => (
                  <label key={r.id} className="check">
                    <input type="checkbox" checked={form.resource_ids.includes(r.id)}
                      onChange={(e) => setForm({ ...form, resource_ids: e.target.checked ? [...form.resource_ids, r.id] : form.resource_ids.filter((x) => x !== r.id) })} />
                    {r.name || r.native_id} <span className="muted small">({PROVIDER_LABEL[r.provider]})</span>
                  </label>
                ))}
                {instances.length === 0 && <span className="muted">Nenhuma instância com ações habilitadas.</span>}
              </fieldset>
            )}
          </div>
          <div className="inline">
            <button type="submit">{editing ? "Salvar agenda" : "Criar agenda"}</button>
            {editing && <button type="button" className="ghost" onClick={() => { setEditing(null); setForm(EMPTY); }}>Cancelar</button>}
            {msg && <span className="error">{msg}</span>}
          </div>
        </form>
      )}

      <div className="budgets">
        {items.map((s) => (
          <div className="card stack-sm" key={s.id}>
            <div className="budget-head">
              <h2>{s.name} {!s.enabled && <span className="pill todo">pausada</span>}</h2>
              {canEdit && (
                <span className="actions">
                  <button className="ghost" onClick={() => toggle(s)}>{s.enabled ? "Pausar" : "Retomar"}</button>
                  <button className="ghost" onClick={() => edit(s)}>Editar</button>
                  <button className="danger" onClick={async () => { await api(`/tenants/${id}/schedules/${s.id}`, { method: "DELETE" }); load(); }}>Excluir</button>
                </span>
              )}
            </div>
            <p>
              {s.days.map((d) => DAYS_PT[d]).join(", ")} · {s.start_time ? `liga ${s.start_time}` : "não liga"} · {s.stop_time ? `desliga ${s.stop_time}` : "não desliga"}
              <span className="muted"> ({s.timezone})</span>
            </p>
            {s.weekly_hours_off !== null && (
              <p><strong>{s.weekly_hours_off} h</strong> desligadas por semana <span className="muted">({Math.round((s.weekly_hours_off / 168) * 100)}% a menos de horas de computação)</span></p>
            )}
            <p className="small">Instâncias: {s.targets.length ? s.targets.map((t) => `${t.name ?? t.id} (${t.state})`).join(", ") : <span className="muted">nenhuma no momento</span>}</p>
            <p className="muted small">Última execução: ligar {s.last_start_on ?? "—"} · desligar {s.last_stop_on ?? "—"}{s.selector_tag ? ` · tag ${s.selector_tag}` : ""}</p>
          </div>
        ))}
        {items.length === 0 && <p className="muted">Nenhuma agenda.</p>}
      </div>
    </div>
  );
}
