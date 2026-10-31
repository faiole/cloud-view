"""Coleta do Azure: subscriptions, inventário (Resource Graph), saúde, custos, Advisor, invoices e ações."""

import gzip
import re
import xml.etree.ElementTree as ET  # noqa: S405 - XML de listagem do Blob Storage da Microsoft, via HTTPS autenticado
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote, urlencode, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.azure.client import STORAGE_SCOPE, Arm, AzureError
from app.collectors.aws_billing import exclusive_owner, months_to_collect
from app.collectors.aws_inventory import SENSITIVE_PORTS, _upsert_event, _Upserter, finding
from app.collectors.common import Ctx, now
from app.models import CloudAccount, Connection, Document, HealthEvent, Recommendation, Resource
from app.services.allocation import active_rules, owner_for_tags, whole_account_owner
from app.services.documents import DocumentRejected, store_document
from app.services.focus_import import ImportErrorInvalid, ingest_rows, read_focus_csv

API = {
    "subscriptions": "2022-12-01",
    "compute": "2024-07-01",
    "health": "2022-10-01",
    "cost": "2023-03-01",
    "advisor": "2023-01-01",
    "billing": "2020-05-01",
}
WORLD = {"*", "internet", "0.0.0.0/0", "0.0.0.0", "any", "::/0"}  # noqa: S104 - origens "qualquer lugar" em regras de NSG


def arm_for(db: Session, ctx: Ctx, conn: Connection) -> Arm:
    cred = ctx.azure.credential_for(db, ctx.vault, conn)
    return ctx.azure.arm(conn.native_id, cred)


def _accounts(db: Session, conn: Connection) -> dict[str, CloudAccount]:
    return {
        a.native_id: a for a in db.scalars(select(CloudAccount).where(CloudAccount.connection_id == conn.id))
    }


# --- Descoberta -----------------------------------------------------------------------------------


def discover(db: Session, ctx: Ctx, conn: Connection) -> dict:
    arm = arm_for(db, ctx, conn)
    subs = arm.list_all("/subscriptions", API["subscriptions"])
    new = []
    for s in subs:
        sid = s["subscriptionId"]
        acc = db.scalar(
            select(CloudAccount).where(CloudAccount.provider == "azure", CloudAccount.native_id == sid)
        )
        if acc is None:
            acc = CloudAccount(provider="azure", native_id=sid, name=s.get("displayName"))
            db.add(acc)
            new.append(sid)
        acc.name = acc.name or s.get("displayName")
        acc.status = "ACTIVE" if s.get("state") == "Enabled" else (s.get("state") or "UNKNOWN").upper()
        acc.connection_id = conn.id
    conn.status, conn.last_error, conn.last_sync_at = "ok", None, now()
    db.flush()
    if not subs:
        conn.last_error = "nenhuma subscription visível: atribua Reader ao app no grupo de gerenciamento ou nas subscriptions"
    return {"subscriptions": len(subs), "new_subscriptions": new}


# --- Inventário -----------------------------------------------------------------------------------

Q_VMS = """Resources | where type =~ 'microsoft.compute/virtualmachines'
| project id, name, location, tags, resourceGroup,
  vmSize = tostring(properties.hardwareProfile.vmSize),
  osType = tostring(properties.storageProfile.osDisk.osType),
  image = tostring(properties.storageProfile.imageReference.offer),
  powerState = tostring(properties.extended.instanceView.powerState.code),
  nics = properties.networkProfile.networkInterfaces,
  priority = tostring(properties.priority), zones"""
Q_NICS = """Resources | where type =~ 'microsoft.network/networkinterfaces'
| project id, ipConfigurations = properties.ipConfigurations,
  nsg = tostring(properties.networkSecurityGroup.id), vm = tostring(properties.virtualMachine.id)"""
Q_PIPS = """Resources | where type =~ 'microsoft.network/publicipaddresses'
| project id, name, location, tags, ip = tostring(properties.ipAddress),
  ipConfig = tostring(properties.ipConfiguration.id), sku = tostring(sku.name),
  allocation = tostring(properties.publicIPAllocationMethod)"""
Q_DISKS = """Resources | where type =~ 'microsoft.compute/disks'
| project id, name, location, tags, sizeGb = toint(properties.diskSizeGB),
  state = tostring(properties.diskState), sku = tostring(sku.name), managedBy = tostring(managedBy)"""
Q_VNETS = """Resources | where type =~ 'microsoft.network/virtualnetworks'
| project id, name, location, tags, prefixes = properties.addressSpace.addressPrefixes, subnets = properties.subnets"""
Q_NSGS = """Resources | where type =~ 'microsoft.network/networksecuritygroups'
| project id, name, location, tags, rules = properties.securityRules"""


def _ports(rule: dict) -> list[tuple[int, int]]:
    out = []
    for r in [rule.get("destinationPortRange")] + list(rule.get("destinationPortRanges") or []):
        if not r:
            continue
        if r == "*":
            out.append((0, 65535))
        elif "-" in r:
            lo, hi = r.split("-", 1)
            out.append((int(lo), int(hi)))
        elif r.isdigit():
            out.append((int(r), int(r)))
    return out


def nsg_findings(rules: list[dict]) -> list[dict]:
    """Regras de entrada 'Allow' vindas de qualquer origem (*, Internet, 0.0.0.0/0)."""
    out = []
    for rule in rules or []:
        p = rule.get("properties", rule)
        if (p.get("direction") or "").lower() != "inbound" or (p.get("access") or "").lower() != "allow":
            continue
        sources = {(p.get("sourceAddressPrefix") or "").lower()} | {
            x.lower() for x in p.get("sourceAddressPrefixes") or []
        }
        if not sources & WORLD:
            continue
        ranges = _ports(p)
        if any(lo == 0 and hi == 65535 for lo, hi in ranges):
            out.append(
                finding(
                    "sg_all_open",
                    "critical",
                    f"Todas as portas abertas para a internet (regra {rule.get('name')})",
                )
            )
            continue
        for port, label in SENSITIVE_PORTS.items():
            if any(lo <= port <= hi for lo, hi in ranges):
                out.append(
                    finding(
                        f"sg_open_{port}",
                        "critical",
                        f"{label} (porta {port}) aberto para a internet (regra {rule.get('name')})",
                    )
                )
    return out


def _lid(x: str | None) -> str | None:
    return x.lower() if x else None


def _tags(x) -> dict:
    return {str(k): "" if v is None else str(v) for k, v in (x or {}).items()}


def collect_inventory(db: Session, ctx: Ctx, conn: Connection, acc: CloudAccount) -> dict:
    arm = arm_for(db, ctx, conn)
    subs = [acc.native_id]
    up = _Upserter(db, acc, active_rules(db, acc.id, now().date()))
    counts: dict[str, int] = {}

    def bump(t: str) -> None:
        counts[t] = counts.get(t, 0) + 1

    nsgs = {}
    for n in arm.graph(Q_NSGS, subs):
        f = nsg_findings(n.get("rules") or [])
        nsgs[_lid(n["id"])] = (n, f)
        up.put(
            n["location"],
            "security_group",
            _lid(n["id"]),
            name=n["name"],
            tags=_tags(n.get("tags")),
            attributes={"rules": len(n.get("rules") or [])},
            findings=f,
        )
        bump("security_group")

    subnet_nsg: dict[str, str | None] = {}
    for v in arm.graph(Q_VNETS, subs):
        vid = _lid(v["id"])
        up.put(
            v["location"],
            "vpc",
            vid,
            name=v["name"],
            vpc_id=vid,
            tags=_tags(v.get("tags")),
            state="Succeeded",
            attributes={"cidr": ", ".join(v.get("prefixes") or []), "is_default": False},
            findings=[],
        )
        bump("vpc")
        for sn in v.get("subnets") or []:
            sp = sn.get("properties", {})
            sid = _lid(sn["id"])
            subnet_nsg[sid] = _lid((sp.get("networkSecurityGroup") or {}).get("id"))
            up.put(
                v["location"],
                "subnet",
                sid,
                name=sn.get("name"),
                vpc_id=vid,
                subnet_id=sid,
                tags={},
                attributes={
                    "cidr": sp.get("addressPrefix") or ", ".join(sp.get("addressPrefixes") or []),
                    "az": v["location"],
                    "available_ips": None,
                    "public_ip_on_launch": False,
                    "nsg": subnet_nsg[sid],
                },
                findings=[],
            )
            bump("subnet")

    pips = {}
    for p in arm.graph(Q_PIPS, subs):
        pid = _lid(p["id"])
        pips[pid] = p
        f = []
        if not p.get("ipConfig"):
            f.append(finding("public_ip_unassociated", "warning", "IP público sem uso e cobrando"))
        if (p.get("sku") or "").lower() == "basic":
            f.append(
                finding(
                    "public_ip_basic_sku",
                    "warning",
                    "SKU Basic foi aposentado pela Microsoft; migre para Standard",
                )
            )
        up.put(
            p["location"],
            "public_ip",
            pid,
            name=p["name"],
            state="associated" if p.get("ipConfig") else "unassociated",
            public_ips=[p["ip"]] if p.get("ip") else [],
            tags=_tags(p.get("tags")),
            attributes={
                "sku": p.get("sku"),
                "allocation": p.get("allocation"),
                "attached_to": p.get("ipConfig"),
            },
            findings=f,
        )
        bump("public_ip")

    nics = {_lid(n["id"]): n for n in arm.graph(Q_NICS, subs)}
    sizes: dict[str, dict[str, tuple[int, int]]] = {}
    for vm in arm.graph(Q_VMS, subs):
        loc = vm["location"]
        if loc not in sizes:
            try:
                sizes[loc] = {
                    s["name"].lower(): (s.get("numberOfCores"), s.get("memoryInMB"))
                    for s in arm.list_all(
                        f"/subscriptions/{acc.native_id}/providers/Microsoft.Compute/locations/{loc}/vmSizes",
                        API["compute"],
                    )
                }
            except AzureError:
                sizes[loc] = {}
        private, public, nsg_ids, subnet, vnet = [], [], set(), None, None
        for ref in vm.get("nics") or []:
            nic = nics.get(_lid(ref.get("id")))
            if not nic:
                continue
            if nic.get("nsg"):
                nsg_ids.add(_lid(nic["nsg"]))
            for cfg in nic.get("ipConfigurations") or []:
                cp = cfg.get("properties", {})
                if cp.get("privateIPAddress"):
                    private.append(cp["privateIPAddress"])
                pub = pips.get(_lid((cp.get("publicIPAddress") or {}).get("id")))
                if pub and pub.get("ip"):
                    public.append(pub["ip"])
                sid = _lid((cp.get("subnet") or {}).get("id"))
                if sid:
                    subnet = subnet or sid
                    vnet = vnet or sid.split("/subnets/")[0]
                    if subnet_nsg.get(sid):
                        nsg_ids.add(subnet_nsg[sid])
        state = (vm.get("powerState") or "").replace("PowerState/", "") or None
        tags = _tags(vm.get("tags"))
        f = []
        if public:
            for nid in nsg_ids:
                for x in nsgs.get(nid, (None, []))[1]:
                    f.append(finding(x["code"], x["severity"], f"{x['message']} em {nsgs[nid][0]['name']}"))
            if not nsg_ids:
                f.append(
                    finding("sg_all_open", "critical", "IP público sem nenhum NSG na placa de rede ou subnet")
                )
        if state == "stopped":
            f.append(
                finding(
                    "vm_stopped_not_deallocated",
                    "warning",
                    "VM parada sem desalocar continua cobrando computação",
                )
            )
        vcpu, mem = sizes[loc].get((vm.get("vmSize") or "").lower(), (None, None))
        up.put(
            loc,
            "vm",
            _lid(vm["id"]),
            name=vm["name"],
            state=state,
            instance_type=vm.get("vmSize"),
            vcpu=vcpu,
            memory_mib=mem,
            platform=vm.get("osType") or None,
            private_ips=sorted(set(private)),
            public_ips=sorted(set(public)),
            vpc_id=vnet,
            subnet_id=subnet,
            security_groups=[
                {"id": n, "name": nsgs[n][0]["name"] if n in nsgs else n.rsplit("/", 1)[-1]}
                for n in sorted(nsg_ids)
            ],
            tags=tags,
            attributes={
                "resource_group": vm.get("resourceGroup"),
                "image": vm.get("image"),
                "priority": vm.get("priority") or "Regular",
                "zones": vm.get("zones"),
                "actions_enabled": tags.get("cloudview-actions") == "enabled",
            },
            findings=f,
        )
        bump("vm")

    for d in arm.graph(Q_DISKS, subs):
        f = []
        if (d.get("state") or "").lower() == "unattached":
            f.append(finding("disk_unattached", "warning", "Disco sem uso (não anexado) e cobrando"))
        up.put(
            d["location"],
            "disk",
            _lid(d["id"]),
            name=d["name"],
            state=d.get("state"),
            tags=_tags(d.get("tags")),
            attributes={
                "size_gib": d.get("sizeGb"),
                "volume_type": d.get("sku"),
                "attached_to": d.get("managedBy"),
                "encrypted": True,
            },
            findings=f,
        )
        bump("disk")

    regions = sorted({k[0] for k in up.by_key})
    removed = up.prune(regions)
    acc.last_inventory_at, acc.last_error = now(), None
    db.flush()
    return {"counts": counts, "removed": removed, "regions": len(regions)}


# --- Saúde ----------------------------------------------------------------------------------------

AVAILABILITY = {"available": "ok", "unavailable": "impaired", "degraded": "impaired", "unknown": "unknown"}


def collect_health(db: Session, ctx: Ctx, conn: Connection, acc: CloudAccount) -> dict:
    arm = arm_for(db, ctx, conn)
    vms = {
        r.native_id: r
        for r in db.scalars(
            select(Resource).where(Resource.cloud_account_id == acc.id, Resource.type == "vm")
        )
    }
    statuses = arm.list_all(
        f"/subscriptions/{acc.native_id}/providers/Microsoft.ResourceHealth/availabilityStatuses",
        API["health"],
        **{"$filter": "resourceType eq 'Microsoft.Compute/virtualMachines'"},
    )
    impaired, open_ids = 0, set()
    for st in statuses:
        rid = _lid(
            st["id"]
            .split("/providers/microsoft.resourcehealth", 1)[0]
            .split("/providers/Microsoft.ResourceHealth", 1)[0]
        )
        res = vms.get(rid)
        if res is None:
            continue
        p = st.get("properties", {})
        state = (p.get("availabilityState") or "Unknown").lower()
        res.health = AVAILABILITY.get(state, "unknown") if res.state == "running" else "unknown"
        res.health_details = {
            "instance": p.get("availabilityState"),
            "system": p.get("reasonType"),
            "ebs": None,
        }
        if res.health == "impaired":
            impaired += 1
            key = f"{res.native_id}:impaired"
            _upsert_event(
                db,
                acc,
                "azure_resource_health",
                key,
                resource_id=res.id,
                tenant_id=res.tenant_id,
                service="Virtual Machines",
                region=res.region,
                category="issue",
                status="open",
                title=f"VM {res.name} {p.get('availabilityState', '').lower()}",
                description=(p.get("summary") or "")[:2000],
                start_at=_dt(p.get("occuredTime") or p.get("occurredTime")),
            )
            open_ids.add(key)

    events = arm.list_all(
        f"/subscriptions/{acc.native_id}/providers/Microsoft.ResourceHealth/events",
        API["health"],
        queryStartTime=(now() - timedelta(days=7)).strftime("%m/%d/%Y"),
    )
    owner = whole_account_owner(db, acc.id)
    for ev in events:
        p = ev.get("properties", {})
        active = (p.get("status") or "").lower() == "active"
        etype = p.get("eventType") or ""
        services = ", ".join(i.get("impactedService", "") for i in p.get("impact") or [])[:64]
        regions = sorted(
            {
                r.get("impactedRegion", "")
                for i in p.get("impact") or []
                for r in i.get("impactedRegions") or []
            }
        )
        _upsert_event(
            db,
            acc,
            "azure_service_health",
            ev.get("name") or ev["id"],
            tenant_id=owner,
            service=services or None,
            region=", ".join(regions)[:64] or None,
            category={"ServiceIssue": "issue", "PlannedMaintenance": "scheduledChange"}.get(
                etype, "accountNotification"
            ),
            status=("upcoming" if etype == "PlannedMaintenance" else "open") if active else "closed",
            title=(p.get("title") or etype)[:300],
            description=(p.get("summary") or "")[:8000],
            start_at=_dt(p.get("impactStartTime")),
            end_at=_dt(p.get("impactMitigationTime")),
        )

    for ev in db.scalars(
        select(HealthEvent).where(
            HealthEvent.cloud_account_id == acc.id,
            HealthEvent.source == "azure_resource_health",
            HealthEvent.status != "closed",
        )
    ):
        if ev.native_id not in open_ids:
            ev.status, ev.end_at = "closed", ev.end_at or now()
    db.flush()
    return {"vms": len(vms), "impaired": impaired, "service_events": len(events)}


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


# --- Custos ---------------------------------------------------------------------------------------


def _cost_query(arm: Arm, sub: str, kind: str, start: date, end: date) -> list[dict]:
    body = {
        "type": kind,
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ServiceName"},
                {"type": "Dimension", "name": "ResourceLocation"},
            ],
        },
    }
    rows = []
    r = arm.post(f"/subscriptions/{sub}/providers/Microsoft.CostManagement/query", API["cost"], body).json()
    while True:
        props = r.get("properties", {})
        cols = [c["name"] for c in props.get("columns", [])]
        rows += [dict(zip(cols, row, strict=False)) for row in props.get("rows", [])]
        nxt = props.get("nextLink")
        if not nxt:
            return rows
        r = arm._request("POST", nxt, json=body).json()


def _day(v) -> str:
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else s[:10]


def collect_costs(db: Session, ctx: Ctx, conn: Connection, today: date | None = None) -> dict:
    today = today or now().date()
    if conn.cost_source == "focus_blob":
        return collect_focus_blob(db, ctx, conn, today)
    arm = arm_for(db, ctx, conn)
    accounts = {k: v for k, v in _accounts(db, conn).items() if v.status in (None, "ACTIVE")}
    out = {}
    for start, end in months_to_collect(today):
        merged: dict[tuple, dict] = {}
        for sub, acc in accounts.items():
            for kind, field in (("ActualCost", "BilledCost"), ("AmortizedCost", "EffectiveCost")):
                for row in _cost_query(arm, sub, kind, start, end):
                    cost = row.get("Cost", row.get("PreTaxCost", 0)) or 0
                    key = (
                        sub,
                        _day(row.get("UsageDate")),
                        row.get("ServiceName") or "Other",
                        row.get("ResourceLocation") or "",
                        row.get("Currency") or "USD",
                    )
                    m = merged.setdefault(
                        key,
                        {
                            "ProviderName": "Microsoft",
                            "BillingAccountId": conn.native_id,
                            "BillingAccountName": conn.name,
                            "SubAccountId": sub,
                            "SubAccountName": acc.name or "",
                            "ChargePeriodStart": key[1],
                            "ServiceName": key[2],
                            "RegionId": key[3],
                            "ChargeCategory": "Usage",
                            "BillingCurrency": key[4],
                            "BilledCost": "0",
                            "EffectiveCost": "0",
                            "Tags": "",
                        },
                    )
                    m[field] = str(Decimal(m[field]) + Decimal(str(cost)))
        rows = [r for r in merged.values() if Decimal(r["BilledCost"]) or Decimal(r["EffectiveCost"])]
        if not rows:
            out[f"{start:%Y-%m}"] = 0
            continue
        r = ingest_rows(
            db,
            rows,
            filename=f"Azure Cost Management {start:%Y-%m}",
            provider_hint="azure",
            user_email=None,
            source="azure_cost_management",
            replace_range=(start, end),
            connection=conn,
        )
        out[f"{start:%Y-%m}"] = r.batch.stored_rows
    conn.last_sync_at = now()
    return {"source": "azure_cost_management", "rows": out}


def _blob_list(arm: Arm, account_url: str, container: str, prefix: str) -> list[dict]:
    blobs, marker = [], None
    while True:
        q = {"restype": "container", "comp": "list", "prefix": prefix}
        if marker:
            q["marker"] = marker
        r = arm.get_url(
            f"{account_url}/{container}?{urlencode(q)}",
            scope=STORAGE_SCOPE,
            headers={"x-ms-version": "2021-08-06"},
        )
        root = ET.fromstring(r.content)  # noqa: S314
        for b in root.iter("Blob"):
            props = b.find("Properties")
            blobs.append(
                {
                    "name": b.findtext("Name"),
                    "modified": props.findtext("Last-Modified") if props is not None else "",
                }
            )
        marker = root.findtext("NextMarker")
        if not marker:
            return blobs


def collect_focus_blob(db: Session, ctx: Ctx, conn: Connection, today: date) -> dict:
    """Export FOCUS do Cost Management no Blob Storage:
    <prefixo>/<export>/<AAAAMMDD-AAAAMMDD>/<execução>/part_*.csv(.gz) — usa a execução mais recente."""
    u = urlparse(conn.focus_s3_uri or "")
    if u.scheme != "https" or not (u.hostname or "").endswith(".blob.core.windows.net"):
        raise ValueError(
            "local do export deve ser https://<conta>.blob.core.windows.net/<container>/<prefixo>"
        )
    container, _, prefix = u.path.lstrip("/").partition("/")
    account_url = f"https://{u.hostname}"
    arm = arm_for(db, ctx, conn)
    blobs = _blob_list(arm, account_url, container, prefix)
    out = {}
    for start, _ in months_to_collect(today):
        marker = f"/{start:%Y%m}01-"
        month_blobs = [
            b for b in blobs if marker in "/" + b["name"] and b["name"].endswith((".csv", ".csv.gz"))
        ]
        if not month_blobs:
            out[f"{start:%Y-%m}"] = "sem arquivos (use o formato CSV no export FOCUS)"
            continue
        # Cada execução do export cria uma pasta; só a mais recente vale
        runs: dict[str, list[dict]] = {}
        for b in month_blobs:
            head = b["name"].split(marker[1:], 1)[1]
            run = head.split("/")[1] if head.count("/") >= 2 else ""
            runs.setdefault(run, []).append(b)
        latest = max(runs.values(), key=lambda bs: max(_http_date(b["modified"]) for b in bs))

        def rows(latest=latest):
            for b in sorted(latest, key=lambda x: x["name"]):
                data = arm.get_url(
                    f"{account_url}/{container}/{quote(b['name'])}",
                    scope=STORAGE_SCOPE,
                    headers={"x-ms-version": "2021-08-06"},
                ).content
                yield from read_focus_csv(gzip.decompress(data) if b["name"].endswith(".gz") else data)

        _, last = monthrange(start.year, start.month)
        try:
            r = ingest_rows(
                db,
                rows(),
                filename=f"{conn.focus_s3_uri} {start:%Y-%m}",
                provider_hint="azure",
                user_email=None,
                source="azure_focus_blob",
                replace_range=(start, start.replace(day=last)),
                connection=conn,
            )
        except ImportErrorInvalid as e:
            out[f"{start:%Y-%m}"] = f"arquivo inválido: {e}"
            continue
        out[f"{start:%Y-%m}"] = r.batch.stored_rows
    conn.last_sync_at = now()
    return {"source": "azure_focus_blob", "files": out}


def _http_date(v: str) -> datetime:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=now().tzinfo)


# --- Advisor (custo) ------------------------------------------------------------------------------

ADVISOR_SOURCE = "azure_advisor"


def _advisor_action(text: str) -> str:
    t = text.lower()
    if "reserv" in t:
        return "PurchaseReservedInstances"
    if "savings plan" in t:
        return "PurchaseSavingsPlans"
    if "right-size" in t or "rightsize" in t or "resize" in t or "underutilized" in t:
        return "Rightsize"
    if "delete" in t or "unattached" in t or "unused" in t or "idle" in t:
        return "Delete"
    if "shut" in t or "stop" in t:
        return "Stop"
    return "Upgrade"


def collect_advisor(db: Session, ctx: Ctx, conn: Connection) -> dict:
    arm = arm_for(db, ctx, conn)
    accounts = _accounts(db, conn)
    seen, total = set(), 0
    for sub, acc in accounts.items():
        if acc.status not in (None, "ACTIVE"):
            continue
        rules = active_rules(db, acc.id, now().date())
        recs = arm.list_all(
            f"/subscriptions/{sub}/providers/Microsoft.Advisor/recommendations",
            API["advisor"],
            **{"$filter": "Category eq 'Cost'"},
        )
        for it in recs:
            p = it.get("properties", {})
            ext = p.get("extendedProperties") or {}
            rid = _lid((p.get("resourceMetadata") or {}).get("resourceId")) or _lid(it["id"])
            res = db.scalar(
                select(Resource).where(Resource.cloud_account_id == acc.id, Resource.native_id == rid)
            )
            solution = (
                (p.get("shortDescription") or {}).get("solution")
                or p.get("recommendationTypeId")
                or "Recomendação"
            )
            rec = db.scalar(
                select(Recommendation).where(
                    Recommendation.source == ADVISOR_SOURCE, Recommendation.native_id == it["id"]
                )
            )
            if rec is None:
                rec = Recommendation(source=ADVISOR_SOURCE, native_id=it["id"])
                db.add(rec)
            monthly = ext.get("savingsAmount")
            if monthly is None and ext.get("annualSavingsAmount") is not None:
                monthly = Decimal(str(ext["annualSavingsAmount"])) / 12
            rec.cloud_account_id = acc.id
            rec.tenant_id = res.tenant_id if res else owner_for_tags(rules, {})
            rec.action_type = _advisor_action(
                solution + " " + (p.get("shortDescription") or {}).get("problem", "")
            )
            rec.resource_type = p.get("impactedField")
            rec.resource_native_id = rid
            rec.region = ext.get("region") or (res.region if res else None)
            rec.title = f"{solution}: {p.get('impactedValue') or ''}"[:300]
            rec.current = ext.get("currentSku") or ext.get("currentSkuName")
            rec.recommended = ext.get("targetSku") or ext.get("recommendedSkuName") or ext.get("term")
            rec.monthly_savings = Decimal(str(monthly or 0)).quantize(Decimal("0.01"))
            rec.currency = (ext.get("savingsCurrency") or "USD")[:3]
            rec.effort = {"High": "Medium", "Medium": "Low", "Low": "VeryLow"}.get(p.get("impact"), None)
            rec.tags = res.tags if res else {}
            rec.last_seen = now()
            if rec.status == "resolved":
                rec.status = "open"
            seen.add(it["id"])
            total += 1
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
    return {"recommendations": total}


# --- Invoices (experimental: exige leitura na conta de cobrança) --------------------------------


def _safe_download(arm: Arm, url: str) -> bytes:
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or not host.endswith(
        (".blob.core.windows.net", ".microsoft.com", ".azure.com")
    ):
        raise ValueError("URL de download inesperada")
    r = arm.hub.http.get(url, timeout=60)
    r.raise_for_status()
    if len(r.content) > 20 * 1024 * 1024:
        raise ValueError("PDF maior que o limite")
    return r.content


def collect_invoices(
    db: Session, ctx: Ctx, conn: Connection, today: date | None = None, months: int = 3
) -> dict:
    if not conn.billing_account_id:
        return {"skipped": "informe a conta de cobrança do Azure na conexão"}
    today = today or now().date()
    arm = arm_for(db, ctx, conn)
    ba = quote(conn.billing_account_id, safe=":")
    start = (today.replace(day=1) - timedelta(days=31 * months)).replace(day=1)
    invoices = arm.list_all(
        f"/providers/Microsoft.Billing/billingAccounts/{ba}/invoices",
        API["billing"],
        periodStartDate=start.isoformat(),
        periodEndDate=today.isoformat(),
    )
    published, shared, existing = [], [], 0
    for inv in invoices:
        p = inv.get("properties", {})
        number = inv.get("name")
        period_start = _dt(p.get("invoicePeriodStartDate"))
        if not number or period_start is None:
            continue
        period = period_start.date().replace(day=1)
        owner = exclusive_owner(db, conn, period)
        if owner is None:
            shared.append(number)
            continue
        if db.scalar(
            select(Document.id).where(
                Document.tenant_id == owner, Document.provider == "azure", Document.number == number
            )
        ):
            existing += 1
            continue
        r = arm.post(
            f"/providers/Microsoft.Billing/billingAccounts/{ba}/invoices/{quote(number)}/download",
            API["billing"],
        )
        for _ in range(10):
            if r.status_code == 200:
                break
            loc = r.headers.get("Location") or r.headers.get("Azure-AsyncOperation")
            if not loc:
                break
            ctx.azure.sleep(float(r.headers.get("Retry-After", 2)))
            r = arm._request("GET", loc)
        url = (r.json() if r.content else {}).get("url")
        if not url:
            continue
        total = p.get("totalAmount") or p.get("amountDue") or {}
        try:
            store_document(
                db,
                ctx.storage,
                tenant_id=owner,
                data=_safe_download(arm, url),
                filename=f"azure-invoice-{number}.pdf",
                kind="invoice",
                billing_period=period,
                uploaded_by="coleta automática",
                provider="azure",
                number=number,
                issuer="Microsoft",
                amount=Decimal(str(total["value"])) if total.get("value") is not None else None,
                currency=total.get("currency"),
                due_date=_dt(p.get("dueDate")).date() if p.get("dueDate") else None,
            )
        except DocumentRejected:
            existing += 1
            continue
        published.append(number)
    return {"published": published, "already_published": existing, "not_published_shared": shared}


def collect_optimization(db: Session, ctx: Ctx, conn: Connection) -> dict:
    try:
        return {"advisor": collect_advisor(db, ctx, conn)}
    except AzureError as e:
        return {"advisor": {"error": str(e)}}


# --- Ações ----------------------------------------------------------------------------------------

VM_ACTIONS = {"reboot": "restart", "start": "start", "stop": "deallocate"}


def vm_action(db: Session, ctx: Ctx, res: Resource, action: str) -> str:
    """Reiniciar, ligar ou desligar (deallocate: para de cobrar computação). Devolve o estado atual."""
    arm = arm_for(db, ctx, res.account.connection)
    arm.post(f"{res.native_id}/{VM_ACTIONS[action]}", API["compute"])
    view = arm.get(f"{res.native_id}/instanceView", API["compute"])
    codes = [s.get("code", "") for s in view.get("statuses", [])]
    power = next((c.split("/", 1)[1] for c in codes if c.startswith("PowerState/")), None)
    return power or {"reboot": "restarting", "start": "starting", "stop": "deallocating"}[action]
