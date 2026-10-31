"""Coleta da OCI: compartments, inventário, rede, manutenções, Announcements, custos, Cloud Advisor e ações."""

import gzip
from calendar import monthrange
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collectors.aws_billing import months_to_collect
from app.collectors.aws_inventory import SENSITIVE_PORTS, _upsert_event, _Upserter, finding
from app.collectors.common import Ctx, now
from app.models import CloudAccount, Connection, CostLine, HealthEvent, Recommendation, Resource
from app.oci.client import Oci, OciError
from app.services.allocation import active_rules, owner_for_tags, whole_account_owner
from app.services.focus_import import ImportErrorInvalid, ingest_rows, read_focus_csv

WORLD = {"0.0.0.0/0", "::/0"}  # noqa: S104 - origens "qualquer lugar" em regras de segurança
PREVIOUS_GEN = ("VM.Standard1.", "VM.Standard2.", "BM.Standard1.", "BM.Standard2.", "VM.DenseIO2.")
USAGE_REPORT_NAMESPACE = "bling"  # namespace da Oracle onde ficam os relatórios de custo de cada tenancy


def client_for(db: Session, ctx: Ctx, conn: Connection) -> Oci:
    return ctx.oci.client(ctx.oci.credential_for(db, ctx.vault, conn))


def _tags(item: dict) -> dict:
    tags = {str(k): str(v) for k, v in (item.get("freeformTags") or item.get("freeform-tags") or {}).items()}
    for ns, kv in (item.get("definedTags") or item.get("defined-tags") or {}).items():
        for k, v in (kv or {}).items():
            tags[f"{ns}.{k}"] = str(v)
    return tags


def regions_of(conn: Connection) -> list[str]:
    return list(conn.regions or [conn.home_region])


# --- Descoberta -----------------------------------------------------------------------------------


def discover(db: Session, ctx: Ctx, conn: Connection) -> dict:
    oci = client_for(db, ctx, conn)
    home = conn.home_region
    subs = oci.list_all("identity", home, f"/tenancies/{conn.native_id}/regionSubscriptions")
    ready = [s["regionName"] for s in subs if s.get("status", "READY") == "READY"]
    for s in subs:
        if s.get("isHomeRegion"):
            conn.home_region = s["regionName"]
    if not conn.regions:
        conn.regions = sorted(ready) or [conn.home_region]
    tenancy = oci.get("identity", conn.home_region, f"/tenancies/{conn.native_id}")
    comps = [
        {"id": conn.native_id, "name": f"{tenancy.get('name', 'tenancy')} (raiz)", "lifecycleState": "ACTIVE"}
    ]
    comps += oci.list_all(
        "identity",
        conn.home_region,
        "/compartments",
        compartmentId=conn.native_id,
        compartmentIdInSubtree="true",
        accessLevel="ANY",
        lifecycleState="ACTIVE",
    )
    new = []
    for c in comps:
        acc = db.scalar(
            select(CloudAccount).where(CloudAccount.provider == "oci", CloudAccount.native_id == c["id"])
        )
        if acc is None:
            acc = CloudAccount(provider="oci", native_id=c["id"], name=c.get("name"))
            db.add(acc)
            new.append(c["id"])
        acc.name = acc.name or c.get("name")
        acc.status = "ACTIVE" if c.get("lifecycleState") == "ACTIVE" else c.get("lifecycleState")
        acc.connection_id = conn.id
    conn.status, conn.last_error, conn.last_sync_at = "ok", None, now()
    db.flush()
    return {"compartments": len(comps), "new_compartments": new, "regions": conn.regions}


# --- Inventário -----------------------------------------------------------------------------------


def _rule_ports(rule: dict) -> list[tuple[int, int]] | None:
    """None = todas as portas. Protocolo 'all', ou TCP/UDP sem faixa, abre tudo."""
    proto = str(rule.get("protocol", "all")).lower()
    if proto == "all":
        return None
    opts = rule.get("tcpOptions") or rule.get("udpOptions")
    if proto in ("6", "17"):
        rng = (opts or {}).get("destinationPortRange")
        if not rng:
            return None
        return [(int(rng.get("min", 0)), int(rng.get("max", 65535)))]
    return []  # ICMP e outros: sem portas


def rules_findings(rules: list[dict], label: str) -> list[dict]:
    out = []
    for r in rules or []:
        if r.get("direction", "INGRESS") != "INGRESS" or r.get("source") not in WORLD:
            continue
        ports = _rule_ports(r)
        if ports is None:
            out.append(
                finding("sg_all_open", "critical", f"Todas as portas abertas para a internet ({label})")
            )
            continue
        for port, name in SENSITIVE_PORTS.items():
            if any(lo <= port <= hi for lo, hi in ports):
                out.append(
                    finding(
                        f"sg_open_{port}",
                        "critical",
                        f"{name} (porta {port}) aberto para a internet ({label})",
                    )
                )
    return out


def collect_inventory(db: Session, ctx: Ctx, conn: Connection, acc: CloudAccount) -> dict:
    oci = client_for(db, ctx, conn)
    up = _Upserter(db, acc, active_rules(db, acc.id, now().date()))
    comp = acc.native_id
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    def bump(t: str) -> None:
        counts[t] = counts.get(t, 0) + 1

    regions = regions_of(conn)
    for region in regions:
        try:
            _collect_region(oci, region, comp, up, bump)
        except OciError as e:
            errors[region] = str(e)
    removed = up.prune([r for r in regions if r not in errors])
    acc.last_inventory_at = now()
    acc.last_error = "; ".join(f"{r}: {m}" for r, m in errors.items())[:2000] or None
    db.flush()
    return {"counts": counts, "removed": removed, "errors": errors}


def _collect_region(oci: Oci, region: str, comp: str, up: _Upserter, bump) -> None:
    open_rules: dict[str, list[dict]] = {}  # security list / NSG → achados

    for sl in oci.list_all("iaas", region, "/securityLists", compartmentId=comp):
        if sl.get("lifecycleState") == "TERMINATED":
            continue
        f = rules_findings(sl.get("ingressSecurityRules") or [], f"security list {sl.get('displayName')}")
        open_rules[sl["id"]] = f
        up.put(
            region,
            "security_group",
            sl["id"],
            name=sl.get("displayName"),
            vpc_id=sl.get("vcnId"),
            tags=_tags(sl),
            attributes={"kind": "security_list", "ingress_rules": len(sl.get("ingressSecurityRules") or [])},
            findings=f,
        )
        bump("security_group")

    for nsg in oci.list_all("iaas", region, "/networkSecurityGroups", compartmentId=comp):
        if nsg.get("lifecycleState") == "TERMINATED":
            continue
        rules = oci.list_all("iaas", region, f"/networkSecurityGroups/{nsg['id']}/securityRules")
        f = rules_findings(rules, f"NSG {nsg.get('displayName')}")
        open_rules[nsg["id"]] = f
        up.put(
            region,
            "security_group",
            nsg["id"],
            name=nsg.get("displayName"),
            vpc_id=nsg.get("vcnId"),
            tags=_tags(nsg),
            attributes={"kind": "nsg", "ingress_rules": len(rules)},
            findings=f,
        )
        bump("security_group")

    for v in oci.list_all("iaas", region, "/vcns", compartmentId=comp):
        if v.get("lifecycleState") == "TERMINATED":
            continue
        up.put(
            region,
            "vpc",
            v["id"],
            name=v.get("displayName"),
            vpc_id=v["id"],
            state=(v.get("lifecycleState") or "").lower(),
            tags=_tags(v),
            attributes={
                "cidr": ", ".join(v.get("cidrBlocks") or [v.get("cidrBlock", "")]),
                "is_default": False,
            },
            findings=[],
        )
        bump("vpc")

    subnet_lists: dict[str, list[str]] = {}
    for s in oci.list_all("iaas", region, "/subnets", compartmentId=comp):
        if s.get("lifecycleState") == "TERMINATED":
            continue
        subnet_lists[s["id"]] = s.get("securityListIds") or []
        up.put(
            region,
            "subnet",
            s["id"],
            name=s.get("displayName"),
            vpc_id=s.get("vcnId"),
            subnet_id=s["id"],
            state=(s.get("lifecycleState") or "").lower(),
            tags=_tags(s),
            attributes={
                "cidr": s.get("cidrBlock"),
                "az": s.get("availabilityDomain") or "regional",
                "available_ips": None,
                "public_ip_on_launch": not s.get("prohibitPublicIpOnVnic", True),
            },
            findings=[],
        )
        bump("subnet")

    vnics_by_instance: dict[str, list[str]] = {}
    for att in oci.list_all("iaas", region, "/vnicAttachments", compartmentId=comp):
        if att.get("lifecycleState") == "ATTACHED" and att.get("vnicId"):
            vnics_by_instance.setdefault(att["instanceId"], []).append(att["vnicId"])

    for inst in oci.list_all("iaas", region, "/instances", compartmentId=comp):
        state = (inst.get("lifecycleState") or "").lower()
        if state in ("terminated", "terminating"):
            continue
        private, public, subnet, vcn, rule_ids = [], [], None, None, set()
        for vid in vnics_by_instance.get(inst["id"], []):
            vnic = oci.get("iaas", region, f"/vnics/{vid}")
            if vnic.get("privateIp"):
                private.append(vnic["privateIp"])
            if vnic.get("publicIp"):
                public.append(vnic["publicIp"])
            sid = vnic.get("subnetId")
            if sid:
                subnet = subnet or sid
                rule_ids.update(subnet_lists.get(sid, []))
            rule_ids.update(vnic.get("nsgIds") or [])
        if subnet:
            sn = up.by_key.get((region, "subnet", subnet))
            vcn = sn.vpc_id if sn else None
        shape = inst.get("shape") or ""
        cfg = inst.get("shapeConfig") or {}
        tags = _tags(inst)
        f = []
        if public:
            for rid in sorted(rule_ids):
                f.extend(open_rules.get(rid, []))
        if shape.startswith(PREVIOUS_GEN):
            f.append(
                finding(
                    "previous_generation",
                    "info",
                    f"{shape} é de geração antiga; shapes Flex atuais custam menos",
                )
            )
        vcpus = cfg.get("vcpus") or (
            int(cfg["ocpus"] * (1 if ".A1." in shape or "Ampere" in shape else 2))
            if cfg.get("ocpus")
            else None
        )
        up.put(
            region,
            "oci_instance",
            inst["id"],
            name=inst.get("displayName"),
            state=state,
            instance_type=shape,
            vcpu=vcpus,
            memory_mib=int(cfg["memoryInGBs"] * 1024) if cfg.get("memoryInGBs") else None,
            platform=(inst.get("sourceDetails") or {}).get("sourceType"),
            private_ips=sorted(set(private)),
            public_ips=sorted(set(public)),
            vpc_id=vcn,
            subnet_id=subnet,
            security_groups=[
                {
                    "id": r,
                    "name": (
                        up.by_key.get((region, "security_group", r)).name
                        if up.by_key.get((region, "security_group", r))
                        else r
                    ),
                }
                for r in sorted(rule_ids)
            ],
            tags=tags,
            attributes={
                "ad": inst.get("availabilityDomain"),
                "ocpus": cfg.get("ocpus"),
                "time_created": inst.get("timeCreated"),
                "actions_enabled": any(
                    tags.get(t) == "enabled" for t in ("cloudview-actions", "cloudview.actions")
                ),
            },
            findings=f,
        )
        bump("oci_instance")

    attached = {
        a["volumeId"]
        for a in oci.list_all("iaas", region, "/volumeAttachments", compartmentId=comp)
        if a.get("lifecycleState") in ("ATTACHED", "ATTACHING")
    }
    for vol in oci.list_all("iaas", region, "/volumes", compartmentId=comp):
        if vol.get("lifecycleState") in ("TERMINATED", "TERMINATING"):
            continue
        f = (
            []
            if vol["id"] in attached
            else [finding("block_volume_unattached", "warning", "Volume sem uso (não anexado) e cobrando")]
        )
        up.put(
            region,
            "block_volume",
            vol["id"],
            name=vol.get("displayName"),
            state=(vol.get("lifecycleState") or "").lower(),
            tags=_tags(vol),
            attributes={
                "size_gib": vol.get("sizeInGBs"),
                "volume_type": f"vpus {vol.get('vpusPerGB', 10)}",
                "attached_to": None,
                "encrypted": True,
            },
            findings=f,
        )
        bump("block_volume")

    for ip in oci.list_all(
        "iaas", region, "/publicIps", scope="REGION", compartmentId=comp, lifetime="RESERVED"
    ):
        f = (
            []
            if ip.get("assignedEntityId")
            else [finding("reserved_ip_unassigned", "info", "IP público reservado sem uso")]
        )
        up.put(
            region,
            "public_ip",
            ip["id"],
            name=ip.get("displayName") or ip.get("ipAddress"),
            state="associated" if ip.get("assignedEntityId") else "unassociated",
            public_ips=[ip["ipAddress"]] if ip.get("ipAddress") else [],
            tags=_tags(ip),
            attributes={"lifetime": "RESERVED", "attached_to": ip.get("assignedEntityId")},
            findings=f,
        )
        bump("public_ip")


# --- Saúde: manutenções e Announcements --------------------------------------------------------------

MAINT_STATUS = {"SCHEDULED": "upcoming", "STARTED": "open", "PROCESSING": "open"}


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_health(db: Session, ctx: Ctx, conn: Connection, acc: CloudAccount) -> dict:
    oci = client_for(db, ctx, conn)
    instances = {
        r.native_id: r
        for r in db.scalars(
            select(Resource).where(Resource.cloud_account_id == acc.id, Resource.type == "oci_instance")
        )
    }
    maint = 0
    for region in sorted({r.region for r in instances.values()}):
        for ev in oci.list_all("iaas", region, "/instanceMaintenanceEvents", compartmentId=acc.native_id):
            res = instances.get(ev.get("instanceId"))
            if res is None:
                continue
            st = MAINT_STATUS.get(ev.get("lifecycleState"), "closed")
            _upsert_event(
                db,
                acc,
                "oci_maintenance",
                ev["id"],
                resource_id=res.id,
                tenant_id=res.tenant_id,
                service="Compute",
                region=region,
                category="scheduledChange",
                status=st,
                title=f"Manutenção {('reboot' if ev.get('instanceAction') in ('REBOOT_MIGRATION', 'REBOOT') else 'agendada')}: {res.name or res.native_id}",
                description=(ev.get("description") or ev.get("maintenanceReason") or "")[:2000],
                start_at=_dt(ev.get("timeWindowStart")),
                end_at=_dt(ev.get("timeHardDueDate")),
            )
            maint += st != "closed"
        for res in instances.values():
            if res.region == region:
                # A OCI não expõe status check como a AWS; sem sinal de falha, fica "—"
                res.health = None if res.state != "running" else res.health

    ann = 0
    if acc.native_id == conn.native_id:  # Announcements são da tenancy inteira (compartment raiz)
        owner = whole_account_owner(db, acc.id)
        items = oci.list_all(
            "announcements",
            conn.home_region,
            "/announcements",
            compartmentId=conn.native_id,
            lifecycleState="ACTIVE",
        )
        seen = set()
        for a in items:
            atype = a.get("announcementType") or ""
            _upsert_event(
                db,
                acc,
                "oci_announcement",
                a["id"],
                tenant_id=owner,
                service=", ".join(a.get("services") or [])[:64] or None,
                region=", ".join(a.get("affectedRegions") or [])[:64] or None,
                category="scheduledChange"
                if "MAINTENANCE" in atype
                else (
                    "issue"
                    if atype in ("EMERGENCY_MAINTENANCE", "PRODUCTION_EVENT_NOTIFICATION")
                    else "accountNotification"
                ),
                status="upcoming" if "SCHEDULED" in atype else "open",
                title=(a.get("summary") or atype)[:300],
                description=atype,
                start_at=_dt(a.get("timeOneValue")),
                end_at=_dt(a.get("timeTwoValue")),
            )
            seen.add(a["id"])
            ann += 1
        for old in db.scalars(
            select(HealthEvent).where(
                HealthEvent.cloud_account_id == acc.id,
                HealthEvent.source == "oci_announcement",
                HealthEvent.status != "closed",
            )
        ):
            if old.native_id not in seen:
                old.status = "closed"
    db.flush()
    return {"instances": len(instances), "maintenance": maint, "announcements": ann}


# --- Custos ---------------------------------------------------------------------------------------


def collect_costs(db: Session, ctx: Ctx, conn: Connection, today: date | None = None) -> dict:
    today = today or now().date()
    if conn.cost_source == "focus_oci":
        return collect_focus_reports(db, ctx, conn, today)
    oci = client_for(db, ctx, conn)
    out = {}
    for start, end in months_to_collect(today):
        body = {
            "tenantId": conn.native_id,
            "timeUsageStarted": f"{start}T00:00:00Z",
            "timeUsageEnded": f"{end + timedelta(days=1)}T00:00:00Z",
            "granularity": "DAILY",
            "queryType": "COST",
            "groupBy": ["service", "compartmentId", "compartmentName", "region", "currency"],
            "compartmentDepth": 7,
        }
        items = oci.post_all("usage", conn.home_region, "/usage", body)
        rows = []
        for it in items:
            amount = Decimal(str(it.get("computedAmount") or 0))
            if not amount:
                continue
            rows.append(
                {
                    "ProviderName": "Oracle",
                    "BillingAccountId": conn.native_id,
                    "BillingAccountName": conn.name,
                    "SubAccountId": it.get("compartmentId") or conn.native_id,
                    "SubAccountName": it.get("compartmentName") or "",
                    "ChargePeriodStart": (it.get("timeUsageStarted") or str(start))[:10],
                    "ServiceName": it.get("service") or "Other",
                    "RegionId": it.get("region") or "",
                    "ChargeCategory": "Usage",
                    "BillingCurrency": it.get("currency") or "USD",
                    "BilledCost": str(amount),
                    "EffectiveCost": str(amount),
                    "Tags": "",
                }
            )
        if not rows:
            out[f"{start:%Y-%m}"] = 0
            continue
        r = ingest_rows(
            db,
            rows,
            filename=f"OCI Usage API {start:%Y-%m}",
            provider_hint="oci",
            user_email=None,
            source="oci_usage_api",
            replace_range=(start, end),
            connection=conn,
        )
        out[f"{start:%Y-%m}"] = r.batch.stored_rows
    conn.last_sync_at = now()
    return {"source": "oci_usage_api", "rows": out}


def collect_focus_reports(db: Session, ctx: Ctx, conn: Connection, today: date) -> dict:
    """FOCUS reports que a Oracle gera para cada tenancy: n/bling/b/<tenancy>/o/FOCUS Reports/AAAA/MM/DD/*.csv.gz.
    Exige a política 'endorse ... to read objects in tenancy usage-report'."""
    oci = client_for(db, ctx, conn)
    base = f"/n/{USAGE_REPORT_NAMESPACE}/b/{conn.native_id}/o"
    out = {}
    for start, _ in months_to_collect(today):
        prefix = f"FOCUS Reports/{start:%Y}/{start:%m}/"
        names, start_with = [], None
        while True:
            params = {"prefix": prefix, "fields": "name"}
            if start_with:
                params["start"] = start_with
            page = oci.get("objectstorage", conn.home_region, base, **params)
            names += [o["name"] for o in page.get("objects", []) if o["name"].endswith((".csv.gz", ".csv"))]
            start_with = page.get("nextStartWith")
            if not start_with:
                break
        if not names:
            out[f"{start:%Y-%m}"] = "sem relatórios FOCUS no mês"
            continue

        def rows(names=names):
            for n in sorted(names):
                data = oci.request(
                    "GET", Oci.url("objectstorage", conn.home_region, f"{base}/{quote(n, safe='')}")
                ).content
                yield from read_focus_csv(gzip.decompress(data) if n.endswith(".gz") else data)

        _, last = monthrange(start.year, start.month)
        try:
            r = ingest_rows(
                db,
                rows(),
                filename=f"OCI FOCUS {start:%Y-%m}",
                provider_hint="oci",
                user_email=None,
                source="oci_focus",
                replace_range=(start, start.replace(day=last)),
                connection=conn,
            )
        except ImportErrorInvalid as e:
            out[f"{start:%Y-%m}"] = f"arquivo inválido: {e}"
            continue
        out[f"{start:%Y-%m}"] = r.batch.stored_rows
    conn.last_sync_at = now()
    return {"source": "oci_focus", "files": out}


# --- Cloud Advisor ----------------------------------------------------------------------------------

ADVISOR_SOURCE = "oci_cloud_advisor"


def _action_type(text: str) -> str:
    t = text.lower()
    if "idle" in t or "unattached" in t or "delete" in t or "unused" in t:
        return "Delete"
    if "rightsiz" in t or "underutil" in t or "downsize" in t or "shape" in t:
        return "Rightsize"
    if "stop" in t:
        return "Stop"
    return "Upgrade"


def collect_optimization(db: Session, ctx: Ctx, conn: Connection) -> dict:
    oci = client_for(db, ctx, conn)
    try:
        recs = oci.list_all(
            "optimizer",
            conn.home_region,
            "/recommendations",
            compartmentId=conn.native_id,
            compartmentIdInSubtree="true",
        )
        actions = oci.list_all(
            "optimizer",
            conn.home_region,
            "/resourceActions",
            compartmentId=conn.native_id,
            compartmentIdInSubtree="true",
        )
    except OciError as e:
        if e.status in (401, 404) or "NotAuthorized" in str(e) or "enroll" in str(e).lower():
            return {"skipped": "Cloud Advisor não habilitado ou sem permissão (read optimizer-api-family)"}
        raise
    names = {r["id"]: r.get("name") or r.get("description") or "" for r in recs}
    accounts = {
        a.native_id: a for a in db.scalars(select(CloudAccount).where(CloudAccount.connection_id == conn.id))
    }
    currency = _currency(db, conn)
    seen = set()
    for it in actions:
        if it.get("status", "PENDING") != "PENDING":
            continue
        acc = accounts.get(it.get("compartmentId"))
        res = (
            db.scalar(select(Resource).where(Resource.native_id == it.get("resourceId")))
            if it.get("resourceId")
            else None
        )
        rec = db.scalar(
            select(Recommendation).where(
                Recommendation.source == ADVISOR_SOURCE, Recommendation.native_id == it["id"]
            )
        )
        if rec is None:
            rec = Recommendation(source=ADVISOR_SOURCE, native_id=it["id"])
            db.add(rec)
        rname = names.get(it.get("recommendationId"), "")
        desc = (it.get("action") or {}).get("description") or rname
        rec.cloud_account_id = acc.id if acc else None
        rec.tenant_id = (
            res.tenant_id
            if res
            else (owner_for_tags(active_rules(db, acc.id, now().date()), {}) if acc else None)
        )
        rec.action_type = _action_type(f"{rname} {desc}")
        rec.resource_type = it.get("resourceType")
        rec.resource_native_id = it.get("resourceId")
        rec.region = res.region if res else None
        rec.title = f"{rname or 'Cloud Advisor'}: {it.get('name') or it.get('resourceId')}"[:300]
        rec.current = (it.get("extendedMetadata") or {}).get("currentShape") or (
            res.instance_type if res else None
        )
        rec.recommended = ((it.get("extendedMetadata") or {}).get("recommendedShape") or desc or "")[
            :300
        ] or None
        rec.monthly_savings = Decimal(str(it.get("estimatedCostSaving") or 0)).quantize(Decimal("0.01"))
        rec.currency = currency
        rec.tags = res.tags if res else {}
        rec.last_seen = now()
        if rec.status == "resolved":
            rec.status = "open"
        seen.add(it["id"])
    if accounts:
        for old in db.scalars(
            select(Recommendation).where(
                Recommendation.source == ADVISOR_SOURCE,
                Recommendation.cloud_account_id.in_([a.id for a in accounts.values()]),
                Recommendation.status.in_(("open", "done")),
            )
        ):
            if old.native_id not in seen:
                old.status, old.status_by = "resolved", "automático"
    db.flush()
    return {"cloud_advisor": len(seen)}


def _currency(db: Session, conn: Connection) -> str:
    rows = db.execute(
        select(CostLine.currency, func.count())
        .join(CloudAccount, CloudAccount.id == CostLine.cloud_account_id)
        .where(CloudAccount.connection_id == conn.id)
        .group_by(CostLine.currency)
    ).all()
    return Counter(dict(rows)).most_common(1)[0][0] if rows else "USD"


# --- Ações ----------------------------------------------------------------------------------------

INSTANCE_ACTIONS = {"reboot": "SOFTRESET", "start": "START", "stop": "SOFTSTOP"}


def instance_action(db: Session, ctx: Ctx, res: Resource, action: str) -> str:
    """Reinício e desligamento "suaves" (o sistema operacional encerra normalmente)."""
    oci = client_for(db, ctx, res.account.connection)
    r = oci.post("iaas", res.region, f"/instances/{res.native_id}", None, action=INSTANCE_ACTIONS[action])
    return (r.json().get("lifecycleState") or "").lower() or {
        "reboot": "running",
        "start": "starting",
        "stop": "stopping",
    }[action]
