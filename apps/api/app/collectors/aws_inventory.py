"""Descoberta de contas, inventário (EC2, EBS, IPs, rede) e saúde."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.collectors.common import (
    Ctx,
    account_session,
    client,
    error_text,
    now,
    paginate,
    regions_for,
    tags_of,
)
from app.models import CloudAccount, Connection, HealthEvent, Resource
from app.services.allocation import active_rules, owner_for_tags, reallocate_resources, whole_account_owner

WORLD = {"0.0.0.0/0", "::/0"}
SENSITIVE_PORTS = {
    22: "SSH",
    3389: "RDP",
    3306: "MySQL",
    5432: "PostgreSQL",
    1433: "SQL Server",
    6379: "Redis",
    27017: "MongoDB",
}
PREVIOUS_GEN = ("t1.", "t2.", "m1.", "m2.", "m3.", "m4.", "c1.", "c3.", "c4.", "r3.", "r4.", "i2.", "d2.")


def finding(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


# --- Descoberta -----------------------------------------------------------------------------------


def discover_accounts(db: Session, ctx: Ctx, conn: Connection) -> dict:
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-discovery")
    if conn.is_organization:
        org = client(sess, "organizations")
        found = [
            {"id": a["Id"], "name": a.get("Name"), "status": a.get("Status")}
            for a in paginate(org, "list_accounts", "Accounts")
        ]
    else:
        ident = client(sess, "sts").get_caller_identity()
        found = [{"id": ident["Account"], "name": conn.name, "status": "ACTIVE"}]

    new = []
    for a in found:
        acc = db.scalar(
            select(CloudAccount).where(CloudAccount.provider == "aws", CloudAccount.native_id == a["id"])
        )
        if acc is None:
            acc = CloudAccount(provider="aws", native_id=a["id"], name=a["name"])
            db.add(acc)
            new.append(a["id"])
        if a["name"]:
            acc.name = acc.name or a["name"]
        acc.status = a["status"]
        acc.connection_id = conn.id
    conn.status = "ok"
    conn.last_error = None
    conn.last_sync_at = now()
    db.flush()
    return {"accounts": len(found), "new_accounts": new}


# --- Inventário -----------------------------------------------------------------------------------


def _sg_findings(sg: dict) -> list[dict]:
    out = []
    for perm in sg.get("IpPermissions", []):
        cidrs = {r.get("CidrIp") for r in perm.get("IpRanges", [])} | {
            r.get("CidrIpv6") for r in perm.get("Ipv6Ranges", [])
        }
        if not cidrs & WORLD:
            continue
        proto = perm.get("IpProtocol")
        lo, hi = perm.get("FromPort"), perm.get("ToPort")
        if proto == "-1" or (lo in (None, 0) and hi in (None, 65535)):
            out.append(
                finding("sg_all_open", "critical", "Todas as portas abertas para a internet (0.0.0.0/0)")
            )
            continue
        for port, label in SENSITIVE_PORTS.items():
            if lo is not None and hi is not None and lo <= port <= hi:
                out.append(
                    finding(f"sg_open_{port}", "critical", f"{label} (porta {port}) aberto para a internet")
                )
    return out


class _Upserter:
    def __init__(self, db: Session, acc: CloudAccount, rules):
        self.db, self.acc, self.rules = db, acc, rules
        existing = db.scalars(select(Resource).where(Resource.cloud_account_id == acc.id)).all()
        self.by_key = {(r.region, r.type, r.native_id): r for r in existing}
        self.seen: set[tuple[str, str, str]] = set()
        self.ts = now()

    def put(self, region: str, rtype: str, native_id: str, **fields) -> Resource:
        key = (region, rtype, native_id)
        res = self.by_key.get(key)
        if res is None:
            res = Resource(
                cloud_account_id=self.acc.id, provider="aws", region=region, type=rtype, native_id=native_id
            )
            self.db.add(res)
            self.by_key[key] = res
        for k, v in fields.items():
            setattr(res, k, v)
        res.tenant_id = owner_for_tags(self.rules, res.tags)
        res.collected_at = self.ts
        self.seen.add(key)
        return res

    def prune(self, regions: list[str]) -> int:
        gone = [r for k, r in self.by_key.items() if k[0] in regions and k not in self.seen]
        for r in gone:
            self.db.delete(r)
        return len(gone)


def _instance_specs(ec2, types: set[str]) -> dict[str, tuple[int | None, int | None]]:
    specs: dict[str, tuple[int | None, int | None]] = {}
    items = sorted(types)
    for i in range(0, len(items), 100):
        for t in ec2.describe_instance_types(InstanceTypes=items[i : i + 100]).get("InstanceTypes", []):
            specs[t["InstanceType"]] = (
                t.get("VCpuInfo", {}).get("DefaultVCpus"),
                t.get("MemoryInfo", {}).get("SizeInMiB"),
            )
    return specs


def collect_inventory(db: Session, ctx: Ctx, conn: Connection, acc: CloudAccount) -> dict:
    sess = account_session(ctx, conn, acc.native_id, who="cloudview-inventory")
    regions = regions_for(sess, conn)
    up = _Upserter(db, acc, active_rules(db, acc.id, now().date()))
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    patch_errors: dict[str, str] = {}
    for region in regions:
        try:
            _collect_region(client(sess, "ec2", region), region, up, counts)
        except Exception as e:  # noqa: BLE001 - uma região com erro não interrompe as demais
            errors[region] = error_text(e)
            continue
        try:
            _collect_patches(client(sess, "ssm", region), region, up)
        except Exception as e:  # noqa: BLE001 - SSM é opcional
            patch_errors[region] = error_text(e)
    removed = up.prune([r for r in regions if r not in errors])
    acc.last_inventory_at = now()
    acc.last_error = "; ".join(f"{r}: {m}" for r, m in errors.items())[:2000] or None
    db.flush()
    return {
        "regions": len(regions),
        "counts": counts,
        "removed": removed,
        "errors": errors,
        "patch_errors": patch_errors,
    }


def _collect_region(ec2, region: str, up: _Upserter, counts: dict[str, int]) -> None:
    def bump(t: str) -> None:
        counts[t] = counts.get(t, 0) + 1

    # Rede primeiro: os achados dos security groups entram nas instâncias
    open_sgs: dict[str, list[dict]] = {}
    for sg in paginate(ec2, "describe_security_groups", "SecurityGroups"):
        f = _sg_findings(sg)
        if f:
            open_sgs[sg["GroupId"]] = f
        up.put(
            region,
            "security_group",
            sg["GroupId"],
            name=sg.get("GroupName"),
            vpc_id=sg.get("VpcId"),
            tags=tags_of(sg.get("Tags")),
            attributes={
                "description": sg.get("Description"),
                "ingress_rules": len(sg.get("IpPermissions", [])),
                "egress_rules": len(sg.get("IpPermissionsEgress", [])),
            },
            findings=f,
        )
        bump("security_group")

    for vpc in paginate(ec2, "describe_vpcs", "Vpcs"):
        tags = tags_of(vpc.get("Tags"))
        up.put(
            region,
            "vpc",
            vpc["VpcId"],
            name=tags.get("Name"),
            state=vpc.get("State"),
            vpc_id=vpc["VpcId"],
            tags=tags,
            attributes={"cidr": vpc.get("CidrBlock"), "is_default": vpc.get("IsDefault", False)},
            findings=[],
        )
        bump("vpc")

    for sn in paginate(ec2, "describe_subnets", "Subnets"):
        tags = tags_of(sn.get("Tags"))
        up.put(
            region,
            "subnet",
            sn["SubnetId"],
            name=tags.get("Name"),
            state=sn.get("State"),
            vpc_id=sn.get("VpcId"),
            subnet_id=sn["SubnetId"],
            tags=tags,
            attributes={
                "cidr": sn.get("CidrBlock"),
                "az": sn.get("AvailabilityZone"),
                "available_ips": sn.get("AvailableIpAddressCount"),
                "public_ip_on_launch": sn.get("MapPublicIpOnLaunch", False),
            },
            findings=[],
        )
        bump("subnet")

    instances = [
        i for r in paginate(ec2, "describe_instances", "Reservations") for i in r.get("Instances", [])
    ]
    instances = [i for i in instances if i.get("State", {}).get("Name") != "terminated"]
    specs = _instance_specs(ec2, {i["InstanceType"] for i in instances}) if instances else {}
    for inst in instances:
        tags = tags_of(inst.get("Tags"))
        itype = inst["InstanceType"]
        sgs = [{"id": g["GroupId"], "name": g.get("GroupName")} for g in inst.get("SecurityGroups", [])]
        private = sorted(
            {
                a["PrivateIpAddress"]
                for ni in inst.get("NetworkInterfaces", [])
                for a in ni.get("PrivateIpAddresses", [])
                if a.get("PrivateIpAddress")
            }
            | ({inst["PrivateIpAddress"]} if inst.get("PrivateIpAddress") else set())
        )
        public = [inst["PublicIpAddress"]] if inst.get("PublicIpAddress") else []
        f: list[dict] = []
        if public:
            for g in sgs:
                for x in open_sgs.get(g["id"], []):
                    f.append(finding(x["code"], x["severity"], f"{x['message']} em {g['name'] or g['id']}"))
        if itype.startswith(PREVIOUS_GEN):
            f.append(
                finding(
                    "previous_generation", "info", f"{itype} é de geração antiga; famílias novas custam menos"
                )
            )
        if inst.get("MetadataOptions", {}).get("HttpTokens") == "optional":
            f.append(finding("imdsv1", "warning", "IMDSv1 habilitado; exija IMDSv2 (HttpTokens=required)"))
        vcpu, mem = specs.get(itype, (None, None))
        launch = inst.get("LaunchTime")
        up.put(
            region,
            "ec2_instance",
            inst["InstanceId"],
            name=tags.get("Name"),
            state=inst.get("State", {}).get("Name"),
            instance_type=itype,
            vcpu=vcpu,
            memory_mib=mem,
            platform=inst.get("PlatformDetails") or inst.get("Platform") or "Linux/UNIX",
            private_ips=private,
            public_ips=public,
            vpc_id=inst.get("VpcId"),
            subnet_id=inst.get("SubnetId"),
            security_groups=sgs,
            tags=tags,
            attributes={
                "az": inst.get("Placement", {}).get("AvailabilityZone"),
                "image_id": inst.get("ImageId"),
                "launch_time": launch.isoformat() if isinstance(launch, datetime) else launch,
                "architecture": inst.get("Architecture"),
                "lifecycle": inst.get("InstanceLifecycle", "on-demand"),
                "actions_enabled": tags.get("cloudview-actions") == "enabled",
            },
            findings=f,
        )
        bump("ec2_instance")

    for vol in paginate(ec2, "describe_volumes", "Volumes"):
        tags = tags_of(vol.get("Tags"))
        f = []
        if vol.get("State") == "available":
            f.append(finding("volume_unattached", "warning", "Volume sem uso (não anexado) e cobrando"))
        if vol.get("VolumeType") == "gp2":
            f.append(finding("gp2_to_gp3", "info", "Migrar de gp2 para gp3 custa ~20% menos"))
        if not vol.get("Encrypted"):
            f.append(finding("volume_unencrypted", "warning", "Volume sem criptografia"))
        att = vol.get("Attachments") or [{}]
        up.put(
            region,
            "ebs_volume",
            vol["VolumeId"],
            name=tags.get("Name"),
            state=vol.get("State"),
            tags=tags,
            attributes={
                "size_gib": vol.get("Size"),
                "volume_type": vol.get("VolumeType"),
                "iops": vol.get("Iops"),
                "encrypted": vol.get("Encrypted", False),
                "attached_to": att[0].get("InstanceId"),
                "az": vol.get("AvailabilityZone"),
            },
            findings=f,
        )
        bump("ebs_volume")

    for addr in ec2.describe_addresses().get("Addresses", []):
        native = addr.get("AllocationId") or addr.get("PublicIp")
        tags = tags_of(addr.get("Tags"))
        f = (
            []
            if addr.get("AssociationId")
            else [finding("eip_unassociated", "warning", "IP elástico sem uso e cobrando")]
        )
        up.put(
            region,
            "elastic_ip",
            native,
            name=tags.get("Name") or addr.get("PublicIp"),
            state="associated" if addr.get("AssociationId") else "unassociated",
            public_ips=[addr["PublicIp"]] if addr.get("PublicIp") else [],
            private_ips=[addr["PrivateIpAddress"]] if addr.get("PrivateIpAddress") else [],
            tags=tags,
            attributes={"instance_id": addr.get("InstanceId"), "domain": addr.get("Domain")},
            findings=f,
        )
        bump("elastic_ip")


# --- Atualizações (SSM Patch Manager) ------------------------------------------------------------


def _collect_patches(ssm, region: str, up: _Upserter) -> None:
    instances = {
        k[2]: r for k, r in up.by_key.items() if k[0] == region and k[1] == "ec2_instance" and k in up.seen
    }
    if not instances:
        return
    managed = {
        i["InstanceId"] for i in paginate(ssm, "describe_instance_information", "InstanceInformationList")
    }
    states = {}
    ids = sorted(managed & instances.keys())
    for i in range(0, len(ids), 50):
        for st in paginate(
            ssm, "describe_instance_patch_states", "InstancePatchStates", InstanceIds=ids[i : i + 50]
        ):
            states[st["InstanceId"]] = st
    for iid, res in instances.items():
        findings = [f for f in res.findings or [] if not f["code"].startswith(("patches_", "ssm_"))]
        if iid not in managed:
            patch = {"managed": False}
            if res.state == "running":
                findings.append(
                    finding("ssm_unmanaged", "info", "Sem agente SSM: não dá para acompanhar atualizações")
                )
        else:
            st = states.get(iid, {})
            end = st.get("OperationEndTime")
            patch = {
                "managed": True,
                "critical": st.get("CriticalNonCompliantCount", 0),
                "security": st.get("SecurityNonCompliantCount", 0),
                "missing": st.get("MissingCount", 0),
                "failed": st.get("FailedCount", 0),
                "pending_reboot": st.get("InstalledPendingRebootCount", 0),
                "last_scan": end.isoformat() if isinstance(end, datetime) else end,
                "baseline": st.get("BaselineId"),
            }
            if patch["critical"]:
                findings.append(
                    finding(
                        "patches_missing_critical",
                        "critical",
                        f"{patch['critical']} atualizações críticas pendentes",
                    )
                )
            if patch["security"]:
                findings.append(
                    finding(
                        "patches_missing_security",
                        "warning",
                        f"{patch['security']} atualizações de segurança pendentes",
                    )
                )
            if patch["failed"]:
                findings.append(
                    finding("patches_failed", "warning", f"{patch['failed']} atualizações falharam")
                )
        res.attributes = {**(res.attributes or {}), "patch": patch}
        res.findings = findings


# --- Saúde ----------------------------------------------------------------------------------------

EVENT_LABELS = {
    "instance-reboot": "Reinicialização agendada da instância",
    "system-reboot": "Reinicialização agendada do host",
    "system-maintenance": "Manutenção agendada do host",
    "instance-retirement": "Aposentadoria da instância agendada",
    "instance-stop": "Parada agendada da instância",
}


def _health_status(st: dict) -> str:
    inst = st.get("InstanceStatus", {}).get("Status")
    sysst = st.get("SystemStatus", {}).get("Status")
    ebs = st.get("AttachedEbsStatus", {}).get("Status")
    states = {inst, sysst, ebs}
    if "impaired" in states:
        return "impaired"
    if "initializing" in states:
        return "initializing"
    if inst == "ok" and sysst == "ok":
        return "ok"
    return "unknown"


def _upsert_event(db: Session, acc: CloudAccount, source: str, native_id: str, **fields) -> HealthEvent:
    ev = db.scalar(
        select(HealthEvent).where(
            HealthEvent.source == source,
            HealthEvent.native_id == native_id,
            HealthEvent.cloud_account_id == acc.id,
        )
    )
    if ev is None:
        ev = HealthEvent(source=source, native_id=native_id, cloud_account_id=acc.id)
        db.add(ev)
    for k, v in fields.items():
        setattr(ev, k, v)
    ev.updated_at = now()
    return ev


def collect_health(db: Session, ctx: Ctx, conn: Connection, acc: CloudAccount) -> dict:
    """Status checks e eventos agendados das instâncias, nas regiões onde há instâncias."""
    sess = account_session(ctx, conn, acc.native_id, who="cloudview-health")
    instances = {
        (r.region, r.native_id): r
        for r in db.scalars(
            select(Resource).where(Resource.cloud_account_id == acc.id, Resource.type == "ec2_instance")
        )
    }
    regions = sorted({k[0] for k in instances})
    open_ids: set[str] = set()
    impaired = 0
    for region in regions:
        ec2 = client(sess, "ec2", region)
        for st in paginate(ec2, "describe_instance_status", "InstanceStatuses", IncludeAllInstances=True):
            res = instances.get((region, st["InstanceId"]))
            if res is None:
                continue
            status = _health_status(st) if st.get("InstanceState", {}).get("Name") == "running" else "unknown"
            res.health = status
            res.health_details = {
                "instance": st.get("InstanceStatus", {}).get("Status"),
                "system": st.get("SystemStatus", {}).get("Status"),
                "ebs": st.get("AttachedEbsStatus", {}).get("Status"),
            }
            if status == "impaired":
                impaired += 1
                key = f"{res.native_id}:impaired"
                _upsert_event(
                    db,
                    acc,
                    "ec2_status",
                    key,
                    resource_id=res.id,
                    tenant_id=res.tenant_id,
                    service="EC2",
                    region=region,
                    category="issue",
                    status="open",
                    title=f"Falha no status check de {res.name or res.native_id}",
                    description=f"instância: {res.health_details['instance']}, host: {res.health_details['system']}",
                )
                open_ids.add(key)
            for e in st.get("Events", []):
                desc = e.get("Description", "")
                done = desc.startswith("[Completed]") or desc.startswith("[Canceled]")
                key = e.get("InstanceEventId") or f"{res.native_id}:{e.get('Code')}:{e.get('NotBefore')}"
                _upsert_event(
                    db,
                    acc,
                    "ec2_scheduled",
                    key,
                    resource_id=res.id,
                    tenant_id=res.tenant_id,
                    service="EC2",
                    region=region,
                    category="scheduledChange",
                    status="closed" if done else "upcoming",
                    title=f"{EVENT_LABELS.get(e.get('Code'), e.get('Code'))}: {res.name or res.native_id}",
                    description=desc,
                    start_at=e.get("NotBefore"),
                    end_at=e.get("NotAfter"),
                )
                if not done:
                    open_ids.add(key)

    # Eventos que sumiram (status voltou a ok, manutenção concluída) são fechados
    for ev in db.scalars(
        select(HealthEvent).where(
            HealthEvent.cloud_account_id == acc.id,
            HealthEvent.source.in_(("ec2_status", "ec2_scheduled")),
            HealthEvent.status != "closed",
        )
    ):
        if ev.native_id not in open_ids:
            ev.status = "closed"
            ev.end_at = ev.end_at or now()
    db.flush()
    return {"regions": len(regions), "instances": len(instances), "impaired": impaired}


def collect_aws_health(db: Session, ctx: Ctx, conn: Connection) -> dict:
    """Eventos do AWS Health. Na Organization usa a visão organizacional; exige plano de suporte
    Business/Enterprise (sem ele a AWS recusa a API e o coletor apenas registra o motivo)."""
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-health")
    h = client(sess, "health", "us-east-1")
    accounts = {
        a.native_id: a for a in db.scalars(select(CloudAccount).where(CloudAccount.connection_id == conn.id))
    }
    flt = {"eventStatusCodes": ["open", "upcoming"]}
    seen: set[tuple[str, str]] = set()
    try:
        if conn.is_organization:
            events = list(paginate(h, "describe_events_for_organization", "events", filter=flt))
            pairs = []
            for ev in events:
                for acc_id in paginate(
                    h, "describe_affected_accounts_for_organization", "affectedAccounts", eventArn=ev["arn"]
                ):
                    pairs.append((ev, acc_id))
        else:
            events = list(paginate(h, "describe_events", "events", filter=flt))
            pairs = [(ev, conn.native_id) for ev in events]
    except Exception as e:  # noqa: BLE001
        msg = error_text(e)
        if "SubscriptionRequired" in msg or "OrganizationalView" in msg or "AWSOrganizationsNotInUse" in msg:
            return {
                "skipped": "AWS Health API indisponível (requer suporte Business/Enterprise ou visão organizacional)"
            }
        raise

    descriptions: dict[str, str] = {}
    arns = sorted({ev["arn"] for ev, _ in pairs})
    for i in range(0, len(arns), 10):
        try:
            resp = h.describe_event_details(eventArns=arns[i : i + 10])
            for d in resp.get("successfulSet", []):
                descriptions[d["event"]["arn"]] = d.get("eventDescription", {}).get("latestDescription", "")
        except Exception:  # noqa: BLE001, S112 - descrição é opcional
            continue

    for ev, acc_id in pairs:
        acc = accounts.get(acc_id)
        if acc is None:
            continue
        _upsert_event(
            db,
            acc,
            "aws_health",
            ev["arn"],
            tenant_id=whole_account_owner(db, acc.id),
            service=ev.get("service"),
            region=ev.get("region"),
            category=ev.get("eventTypeCategory"),
            status=ev.get("statusCode", "open"),
            title=ev.get("eventTypeCode", "AWS Health"),
            description=(descriptions.get(ev["arn"]) or "")[:8000],
            start_at=ev.get("startTime"),
            end_at=ev.get("endTime"),
        )
        seen.add((ev["arn"], acc.id))

    acc_ids = [a.id for a in accounts.values()]
    if acc_ids:
        for old in db.scalars(
            select(HealthEvent).where(
                HealthEvent.source == "aws_health",
                HealthEvent.cloud_account_id.in_(acc_ids),
                HealthEvent.status != "closed",
            )
        ):
            if (old.native_id, old.cloud_account_id) not in seen:
                old.status = "closed"
    db.flush()
    return {"events": len(arns), "affected": len(pairs)}


def clear_account_resources(db: Session, acc: CloudAccount) -> None:
    db.execute(delete(Resource).where(Resource.cloud_account_id == acc.id))
    reallocate_resources(db, acc.id)
