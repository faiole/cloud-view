"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import Bars from "@/components/Bars";
import { api, can, money, type CostSummary, type Tenant, type Total } from "@/lib/api";
import { useMe } from "@/lib/useMe";

function TotalsValue({ totals }: { totals: Total[] }) {
  if (!totals.length) return <>—</>;
  return <>{totals.map((t) => money(t.cost, t.currency)).join(" + ")}</>;
}

export default function Custos() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [s, setS] = useState<CostSummary | null>(null);
  const [t, setT] = useState<Tenant | null>(null);

  useEffect(() => {
    if (can(me, id, "costs.read")) api<CostSummary>(`/tenants/${id}/costs/summary`).then(setS);
    if (me?.is_admin) api<Tenant>(`/tenants/${id}`).then(setT);
  }, [id, me]);

  if (!can(me, id, "costs.read")) return <p className="muted">Seu papel não inclui custos.</p>;

  return (
    <div className="stack">
      <div className="grid">
        <div className="card kpi"><div className="label">Mês atual (até hoje)</div><div className="value"><TotalsValue totals={s?.month_to_date ?? []} /></div></div>
        <div className="card kpi"><div className="label">Mês anterior</div><div className="value"><TotalsValue totals={s?.previous_month ?? []} /></div></div>
        <div className="card kpi"><div className="label">Contas com custo</div><div className="value">{s?.accounts ?? "—"}</div></div>
      </div>
      <div className="grid wide">
        <div className="card"><h2>Últimos 12 meses</h2><Bars rows={s?.monthly ?? []} /></div>
        <div className="card"><h2>Principais serviços no mês</h2><Bars rows={s?.top_services ?? []} max={8} /></div>
      </div>
      {t && (
        <div className="card">
          <h2>ExternalId AWS</h2>
          <p className="muted">Use na trust policy das roles criadas pelo template de onboarding (<code>onboarding/aws</code>).</p>
          <p><code>{t.aws_external_id}</code></p>
        </div>
      )}
    </div>
  );
}
