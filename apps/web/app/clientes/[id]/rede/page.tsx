"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import Findings from "@/components/Findings";
import { api, type NetworkSummary } from "@/lib/api";

const CODE_LABEL: Record<string, string> = {
  sg_open_22: "SSH aberto para a internet",
  sg_open_3389: "RDP aberto para a internet",
  sg_all_open: "Todas as portas abertas",
  eip_unassociated: "IPs elásticos sem uso",
  volume_unattached: "Volumes sem uso",
  gp2_to_gp3: "Volumes gp2 (migrar para gp3)",
  previous_generation: "Instâncias de geração antiga",
  imdsv1: "IMDSv1 habilitado",
  volume_unencrypted: "Volumes sem criptografia",
};

export default function Rede() {
  const { id } = useParams<{ id: string }>();
  const [n, setN] = useState<NetworkSummary | null>(null);
  useEffect(() => { api<NetworkSummary>(`/tenants/${id}/network`).then(setN); }, [id]);

  const findings = Object.entries(n?.findings_by_code ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <div className="stack">
      {findings.length > 0 && (
        <div className="card">
          <h2>Alertas e dicas</h2>
          <div className="chips">
            {findings.map(([code, count]) => (
              <span key={code} className={`chip ${code.startsWith("sg_") ? "chip-bad" : ""}`}>{CODE_LABEL[code] ?? code} <b>{count}</b></span>
            ))}
          </div>
        </div>
      )}

      {(n?.vpcs ?? []).map((v) => (
        <div className="card stack-sm" key={v.vpc.id}>
          <h2>{v.vpc.name || v.vpc.native_id} <span className="muted small mono">{String(v.vpc.attributes.cidr)} · {v.vpc.account_native_id} · {v.vpc.region}{v.vpc.attributes.is_default ? " · padrão" : ""}</span></h2>
          <p className="muted">{v.instances} instâncias · {v.subnets.length} subnets</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Subnet</th><th>CIDR</th><th>Zona</th><th>IPs livres</th><th>IP público automático</th></tr></thead>
              <tbody>
                {v.subnets.map((s) => (
                  <tr key={s.id}>
                    <td>{s.name || <span className="mono">{s.native_id}</span>}</td>
                    <td className="mono">{String(s.attributes.cidr)}</td>
                    <td>{String(s.attributes.az)}</td>
                    <td className="num">{String(s.attributes.available_ips)}</td>
                    <td>{s.attributes.public_ip_on_launch ? <span className="pill todo">sim</span> : "não"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {v.exposed_instances.length > 0 && (
            <div>
              <strong className="error">Instâncias expostas à internet</strong>
              <ul>{v.exposed_instances.map((i) => <li key={i.id}>{i.name || i.native_id} · <span className="mono">{i.public_ips.join(", ")}</span></li>)}</ul>
            </div>
          )}
        </div>
      ))}

      {n && n.open_security_groups.length > 0 && (
        <div className="card table-wrap">
          <h2>Security groups abertos para a internet</h2>
          <table>
            <thead><tr><th>Grupo</th><th>VPC</th><th>Problema</th></tr></thead>
            <tbody>
              {n.open_security_groups.map((g) => (
                <tr key={g.id}>
                  <td><strong>{g.name}</strong><br /><span className="muted mono">{g.native_id}</span></td>
                  <td className="mono">{g.vpc_id}</td>
                  <td><Findings items={g.findings} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {n && n.vpcs.length === 0 && <p className="muted">Nenhuma rede coletada ainda para esta empresa.</p>}
    </div>
  );
}
