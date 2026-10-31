"""Fase 1: coleta AWS com o simulador moto (EC2, Organizations, STS, S3) e fakes para
Cost Explorer, Health e Invoicing, que o moto não cobre bem."""

import gzip
from datetime import date
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from app.aws.session import AwsHub
from app.collectors import aws_billing, aws_inventory
from app.collectors.common import Ctx
from app.core import db as dbmod
from app.models import ActionRequest, CloudAccount, Connection, CostLine, Job, Resource
from app.services import scheduler
from app.services.storage import LocalStorage
from tests.helpers import admin
from tests.test_documents import PDF

MOTO_ACCOUNT = "123456789012"


@pytest.fixture
def aws(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


@pytest.fixture
def ctx(tmp_path):
    return Ctx(
        hub=AwsHub(boto3.Session(region_name="us-east-1")), storage=LocalStorage(str(tmp_path / "inv"))
    )


def _db():
    db = dbmod.get_sessionmaker()()
    dbmod.apply_tenant_scope(db, tenant_ids=[], is_admin=True)
    return db


def _make_ec2():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vpc = ec2.create_vpc(CidrBlock="10.10.0.0/16")["Vpc"]["VpcId"]
    ec2.create_tags(Resources=[vpc], Tags=[{"Key": "Name", "Value": "vpc-producao"}])
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.10.1.0/24")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="web", Description="web", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg,
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        ],
    )

    def run(name, owner, actions=False, itype="t3.medium"):
        tags = [{"Key": "Name", "Value": name}, {"Key": "cost-center", "Value": owner}]
        if actions:
            tags.append({"Key": "cloudview-actions", "Value": "enabled"})
        return ec2.run_instances(
            ImageId="ami-12c6146b",
            InstanceType=itype,
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet,
            SecurityGroupIds=[sg],
            NetworkInterfaces=[],
            TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
        )["Instances"][0]["InstanceId"]

    a = run("erp-app", "empresa-a", actions=True)
    b = run("site-b", "empresa-b", itype="t2.micro")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=50, VolumeType="gp2")
    ec2.allocate_address(Domain="vpc")
    return {"vpc": vpc, "inst_a": a, "inst_b": b}


@pytest.fixture
def setup(client, oidc, aws):
    """Conta avulsa (a conta padrão do moto) dividida pela tag cost-center entre A e B."""
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "Empresa A", "slug": "empresa-a"}).json()
    b = client.post("/api/tenants", json={"name": "Empresa B", "slug": "empresa-b"}).json()
    r = client.post(
        "/api/admin/connections",
        json={
            "name": "Conta MSP",
            "kind": "aws_account",
            "account_id": MOTO_ACCOUNT,
            "regions": ["us-east-1"],
        },
    )
    assert r.status_code == 201, r.text
    conn = r.json()
    assert conn["status"] == "pending" and len(conn["external_id"]) == 32
    ids = _make_ec2()
    return {"a": a, "b": b, "conn": conn, **ids}


def _assign_by_tag(client, native_id, tenants):
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == native_id)
    for tid, value in tenants:
        r = client.post(
            f"/api/admin/accounts/{acc['id']}/assignments",
            json={"tenant_id": tid, "tag_key": "cost-center", "tag_value": value},
        )
        assert r.status_code == 201, r.text


def _run(ctx, kind, conn_id, acc_native=None):
    with _db() as db:
        conn = db.get(Connection, conn_id)
        acc = db.query(CloudAccount).filter_by(native_id=acc_native).one() if acc_native else None
        fn = {
            "discover": lambda: aws_inventory.discover_accounts(db, ctx, conn),
            "inventory": lambda: aws_inventory.collect_inventory(db, ctx, conn, acc),
            "health": lambda: aws_inventory.collect_health(db, ctx, conn, acc),
        }[kind]
        out = fn()
        db.commit()
        return out


def test_discovery_inventory_and_company_isolation(client, oidc, setup, ctx):
    s = setup
    out = _run(ctx, "discover", s["conn"]["id"])
    assert out["accounts"] == 1
    _assign_by_tag(client, MOTO_ACCOUNT, [(s["a"]["id"], "empresa-a"), (s["b"]["id"], "empresa-b")])

    inv = _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    assert inv["counts"]["ec2_instance"] == 2 and not inv["errors"]

    a_res = client.get(f"/api/tenants/{s['a']['id']}/resources", params={"type": "ec2_instance"}).json()
    assert [r["native_id"] for r in a_res] == [s["inst_a"]]
    inst = a_res[0]
    assert inst["instance_type"] == "t3.medium" and inst["vcpu"] == 2 and inst["memory_mib"] == 4096
    assert inst["vpc_id"] == s["vpc"] and inst["private_ips"]
    assert inst["attributes"]["actions_enabled"] is True

    b_res = client.get(f"/api/tenants/{s['b']['id']}/resources", params={"type": "ec2_instance"}).json()
    assert [r["native_id"] for r in b_res] == [s["inst_b"]]
    assert any(f["code"] == "previous_generation" for f in b_res[0]["findings"])

    # Volume e IP sem tag de empresa: ficam só com o administrador, com as dicas registradas
    with _db() as db:
        vol = db.query(Resource).filter_by(type="ebs_volume", state="available").one()
        assert vol.tenant_id is None
        assert {f["code"] for f in vol.findings} >= {"volume_unattached", "gp2_to_gp3"}
        eip = db.query(Resource).filter_by(type="elastic_ip").one()
        assert eip.findings[0]["code"] == "eip_unassociated"


def test_network_flags_world_open_ssh(client, oidc, setup, ctx):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == MOTO_ACCOUNT)
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    net = client.get(f"/api/tenants/{s['a']['id']}/network").json()
    assert net["findings_by_code"].get("sg_open_22", 0) >= 1
    assert any(sg["name"] == "web" for sg in net["open_security_groups"])
    assert any(v["vpc"]["native_id"] == s["vpc"] and v["subnets"] for v in net["vpcs"])


def test_inventory_removes_terminated(client, oidc, setup, ctx):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    boto3.client("ec2", region_name="us-east-1").terminate_instances(InstanceIds=[s["inst_b"]])
    out = _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    assert out["removed"] >= 1
    with _db() as db:
        assert db.query(Resource).filter_by(native_id=s["inst_b"]).count() == 0


def test_health_status_checks(client, oidc, setup, ctx):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    _assign_by_tag(client, MOTO_ACCOUNT, [(s["a"]["id"], "empresa-a")])
    _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    out = _run(ctx, "health", s["conn"]["id"], MOTO_ACCOUNT)
    assert out["instances"] == 2
    h = client.get(f"/api/tenants/{s['a']['id']}/health").json()
    assert h["instances"] == 1 and h["running"] == 1 and h["impaired"] == []


def test_organization_discovery(client, oidc, aws, ctx):
    admin(client, oidc)
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    for n in ("empresa-a-prod", "empresa-b-prod"):
        org.create_account(Email=f"{n}@x.com", AccountName=n)
    conn = client.post(
        "/api/admin/connections",
        json={"name": "Organização AB", "kind": "aws_organization", "account_id": MOTO_ACCOUNT},
    ).json()
    out = _run(ctx, "discover", conn["id"])
    assert out["accounts"] == 3 and len(out["new_accounts"]) == 3
    names = {a["name"] for a in client.get("/api/admin/accounts").json()}
    assert {"empresa-a-prod", "empresa-b-prod"} <= names
    assert client.get("/api/admin/connections").json()[0]["status"] == "ok"


def test_connection_reuses_imported_payer(client, oidc, aws):
    from tests.helpers import focus_csv, import_file, line

    admin(client, oidc)
    import_file(client, focus_csv([line(MOTO_ACCOUNT, "100000000001", 1, "EC2", 5)]))
    r = client.post(
        "/api/admin/connections",
        json={"name": "Org", "kind": "aws_organization", "account_id": MOTO_ACCOUNT},
    )
    assert r.status_code == 201
    conns = client.get("/api/admin/connections").json()
    assert (
        len(conns) == 1
        and conns[0]["accounts"] == 1
        and conns[0]["role_arn"].endswith(":role/CloudViewReadOnly")
    )
    again = client.post(
        "/api/admin/connections", json={"name": "Org", "kind": "aws_organization", "account_id": MOTO_ACCOUNT}
    )
    assert again.status_code == 409


def test_cost_explorer_collection(client, oidc, setup, ctx, monkeypatch):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == MOTO_ACCOUNT)
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": s["a"]["id"]})

    def fake_pages(ce, start, end):
        yield {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": f"{start:%Y-%m}-02"},
                    "Groups": [
                        {
                            "Keys": [MOTO_ACCOUNT, "Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": "12.5", "Unit": "USD"},
                                "AmortizedCost": {"Amount": "10.0", "Unit": "USD"},
                            },
                        },
                        {
                            "Keys": [MOTO_ACCOUNT, "Tax"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": "0", "Unit": "USD"},
                                "AmortizedCost": {"Amount": "0", "Unit": "USD"},
                            },
                        },
                    ],
                }
            ]
        }

    monkeypatch.setattr(aws_billing, "fetch_ce_pages", fake_pages)
    today = date(2026, 10, 20)
    with _db() as db:
        conn = db.get(Connection, s["conn"]["id"])
        conn.cost_source = "cost_explorer"
        out = aws_billing.collect_costs(db, ctx, conn, today=today)
        # Rodar de novo não duplica
        aws_billing.collect_costs(db, ctx, conn, today=today)
        db.commit()
    assert out["rows"] == {"2026-10": 1}
    rep = client.get(
        f"/api/tenants/{s['a']['id']}/reports/usage",
        params={"start": "2026-10-01", "end": "2026-10-31", "metric": "effective"},
    ).json()
    assert Decimal(rep["totals"][0]["cost"]) == Decimal("10")


def test_previous_month_only_in_first_week():
    assert [m[0] for m in aws_billing.months_to_collect(date(2026, 10, 3))] == [
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]
    assert [m[0] for m in aws_billing.months_to_collect(date(2026, 10, 20))] == [date(2026, 10, 1)]


def test_focus_s3_collection(client, oidc, setup, ctx):
    from tests.helpers import focus_csv, line

    s = setup
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="billing-exports")
    data = focus_csv(
        [line(MOTO_ACCOUNT, MOTO_ACCOUNT, 5, "Amazon EC2", 40, {"user:cost-center": "empresa-a"})]
    )
    data = data.replace(b"2026-09-05", b"2026-10-05")
    s3.put_object(
        Bucket="billing-exports",
        Key="focus/cloudview/data/BILLING_PERIOD=2026-10/part-0.csv.gz",
        Body=gzip.compress(data),
    )
    s3.put_object(
        Bucket="billing-exports", Key="focus/cloudview/data/BILLING_PERIOD=2026-10/part-0.parquet", Body=b"x"
    )
    r = client.patch(
        f"/api/admin/connections/{s['conn']['id']}",
        json={"cost_source": "focus_s3", "focus_s3_uri": "s3://billing-exports/focus/"},
    )
    assert r.status_code == 200
    _run(ctx, "discover", s["conn"]["id"])
    _assign_by_tag(client, MOTO_ACCOUNT, [(s["a"]["id"], "empresa-a")])
    with _db() as db:
        out = aws_billing.collect_costs(
            db, ctx, db.get(Connection, s["conn"]["id"]), today=date(2026, 10, 20)
        )
        db.commit()
    assert out["files"]["2026-10"] == 1
    with _db() as db:
        line_ = db.query(CostLine).one()
        assert line_.tenant_id == s["a"]["id"] and line_.tags == {"cost-center": "empresa-a"}


class FakeInvoicing:
    def __init__(self, invoices):
        self.invoices = invoices
        self.pdf_calls = []

    def list_invoice_summaries(self, Selector, Filter, **_):
        p = Filter["BillingPeriod"]
        return {"InvoiceSummaries": [i for i in self.invoices if i["BillingPeriod"] == p]}

    def get_invoice_pdf(self, InvoiceId):
        self.pdf_calls.append(InvoiceId)
        return {"InvoicePDF": {"DocumentUrl": f"https://invoicing.s3.amazonaws.com/{InvoiceId}.pdf"}}


def _invoice(n, month):
    return {
        "InvoiceId": f"inv-{n}",
        "CommercialInvoiceId": f"BR{n}",
        "BillingPeriod": {"Month": month, "Year": 2026},
        "PaymentCurrencyAmount": {"TotalAmount": "123.45", "CurrencyCode": "BRL"},
        "Entity": {"InvoicingEntity": "Amazon AWS Serviços Brasil Ltda."},
    }


def test_invoices_published_only_for_exclusive_payer(client, oidc, setup, ctx, monkeypatch):
    from tests.helpers import focus_csv, import_file, line

    s = setup
    # Setembro: só a empresa A tem custo na pagadora. Agosto: A e B (compartilhada).
    rows = [line(MOTO_ACCOUNT, MOTO_ACCOUNT, 3, "EC2", 10, {"user:cost-center": "empresa-a"})]
    aug = [
        {
            **line(MOTO_ACCOUNT, MOTO_ACCOUNT, 3, "EC2", 10, {"user:cost-center": v}),
            "ChargePeriodStart": "2026-08-03T00:00:00Z",
        }
        for v in ("empresa-a", "empresa-b")
    ]
    import_file(client, focus_csv(rows + aug))
    _assign_by_tag(client, MOTO_ACCOUNT, [(s["a"]["id"], "empresa-a"), (s["b"]["id"], "empresa-b")])

    fake = FakeInvoicing([_invoice(1, 9), _invoice(2, 8)])
    real_client = aws_billing.client
    monkeypatch.setattr(
        aws_billing,
        "client",
        lambda sess, svc, region="us-east-1": fake if svc == "invoicing" else real_client(sess, svc, region),
    )
    monkeypatch.setattr(aws_billing, "download_pdf", lambda url: PDF + url.encode())
    with _db() as db:
        conn = db.get(Connection, s["conn"]["id"])
        out = aws_billing.collect_invoices(db, ctx, conn, today=date(2026, 10, 20))
        again = aws_billing.collect_invoices(db, ctx, conn, today=date(2026, 10, 20))
        db.commit()
    assert out["published"] == ["BR1"] and out["not_published_shared_payer"] == ["BR2"]
    assert again["published"] == [] and again["already_published"] == 1
    assert fake.pdf_calls == ["inv-1"]

    docs = client.get(f"/api/tenants/{s['a']['id']}/documents", params={"year": 2026}).json()
    assert len(docs) == 1 and docs[0]["number"] == "BR1" and docs[0]["currency"] == "BRL"
    assert client.get(f"/api/tenants/{s['b']['id']}/documents", params={"year": 2026}).json() == []


def test_download_pdf_rejects_foreign_hosts():
    for url in (
        "http://invoicing.s3.amazonaws.com/x",
        "https://evil.example/x",
        "https://amazonaws.com.evil.io/x",
    ):
        with pytest.raises(ValueError):
            aws_billing.download_pdf(url)


class FakeHealth:
    def get_paginator(self, op):
        data = {
            "describe_events_for_organization": {
                "events": [
                    {
                        "arn": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_MAINT/1",
                        "service": "EC2",
                        "eventTypeCode": "AWS_EC2_INSTANCE_RETIREMENT_SCHEDULED",
                        "eventTypeCategory": "scheduledChange",
                        "region": "us-east-1",
                        "statusCode": "upcoming",
                    }
                ]
            },
            "describe_affected_accounts_for_organization": {"affectedAccounts": [MOTO_ACCOUNT]},
        }[op]

        class P:
            def paginate(self, **_):
                return [data]

        return P()

    def describe_event_details(self, eventArns):
        return {
            "successfulSet": [
                {"event": {"arn": eventArns[0]}, "eventDescription": {"latestDescription": "x"}}
            ]
        }


def test_aws_health_org_events_follow_account_owner(client, oidc, setup, ctx, monkeypatch):
    s = setup
    with _db() as db:
        db.get(Connection, s["conn"]["id"]).kind = "aws_organization"
        db.commit()
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    _run(ctx, "discover", s["conn"]["id"])
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == MOTO_ACCOUNT)
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    real_client = aws_inventory.client
    monkeypatch.setattr(
        aws_inventory,
        "client",
        lambda sess, svc, region="us-east-1": (
            FakeHealth() if svc == "health" else real_client(sess, svc, region)
        ),
    )
    with _db() as db:
        out = aws_inventory.collect_aws_health(db, ctx, db.get(Connection, s["conn"]["id"]))
        db.commit()
    assert out == {"events": 1, "affected": 1}
    ev = client.get(f"/api/tenants/{s['a']['id']}/health").json()["events"]
    assert ev[0]["title"] == "AWS_EC2_INSTANCE_RETIREMENT_SCHEDULED"
    assert client.get(f"/api/tenants/{s['b']['id']}/health").json()["events"] == []


# --- Ações rápidas -------------------------------------------------------------------------------


@pytest.fixture
def collected(client, oidc, setup, ctx):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    _assign_by_tag(client, MOTO_ACCOUNT, [(s["a"]["id"], "empresa-a"), (s["b"]["id"], "empresa-b")])
    _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    res = {r["native_id"]: r for r in client.get(f"/api/tenants/{s['a']['id']}/resources").json()}
    return {**s, "res_a": res[s["inst_a"]]}


def _worker(monkeypatch, tmp_path):
    from app import worker as wmod

    monkeypatch.setattr(wmod, "get_storage", lambda: LocalStorage(str(tmp_path / "w")))
    return wmod.Worker(only={"action"}, schedule=False)


def test_stop_instance_through_worker(client, oidc, collected, monkeypatch, tmp_path):
    s = collected
    r = client.post(
        f"/api/tenants/{s['a']['id']}/resources/{s['res_a']['id']}/actions",
        json={"action": "stop", "confirm": "erp-app", "reason": "janela de manutenção"},
    )
    assert r.status_code == 202, r.text
    assert (
        client.post(
            f"/api/tenants/{s['a']['id']}/resources/{s['res_a']['id']}/actions",
            json={"action": "reboot", "confirm": "erp-app"},
        ).status_code
        == 409
    )  # já existe ação em andamento

    w = _worker(monkeypatch, tmp_path)
    assert w.run_once() is True
    acts = client.get(f"/api/tenants/{s['a']['id']}/actions").json()
    assert acts[0]["status"] == "succeeded" and acts[0]["result"]["state"] in {"stopping", "stopped"}
    state = boto3.client("ec2", region_name="us-east-1").describe_instances(InstanceIds=[s["inst_a"]])
    assert state["Reservations"][0]["Instances"][0]["State"]["Name"] in {"stopping", "stopped"}
    assert any(e["action"] == "action.stop" for e in client.get("/api/audit").json())


def test_action_guards(client, oidc, collected):
    s = collected
    url = f"/api/tenants/{s['a']['id']}/resources/{s['res_a']['id']}/actions"
    assert client.post(url, json={"action": "stop", "confirm": "outro-nome"}).status_code == 422
    assert (
        client.post(url, json={"action": "start", "confirm": "erp-app"}).status_code == 409
    )  # já está ligada

    # Instância de B sem a tag cloudview-actions
    b = client.get(f"/api/tenants/{s['b']['id']}/resources", params={"type": "ec2_instance"}).json()[0]
    r = client.post(
        f"/api/tenants/{s['b']['id']}/resources/{b['id']}/actions",
        json={"action": "reboot", "confirm": "site-b"},
    )
    assert r.status_code == 409 and "cloudview-actions" in r.json()["detail"]

    # Recurso de A pedido pela URL de B
    assert (
        client.post(
            f"/api/tenants/{s['b']['id']}/resources/{s['res_a']['id']}/actions",
            json={"action": "stop", "confirm": "erp-app"},
        ).status_code
        == 404
    )


def test_action_permissions(client, oidc, collected):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import login
    from tests.test_tenants import _expire_mfa

    s = collected
    url = f"/api/tenants/{s['a']['id']}/resources/{s['res_a']['id']}/actions"
    for email, role in (("fin@a.com", "finance"), ("view@a.com", "viewer"), ("ops@a.com", "operator")):
        client.put(f"/api/tenants/{s['a']['id']}/members", json={"email": email, "role": role})
    fin = TestClient(app, base_url="http://testserver")
    login(fin, oidc, sub="fin", email="fin@a.com")
    assert fin.get(f"/api/tenants/{s['a']['id']}/resources").status_code == 403
    view = TestClient(app, base_url="http://testserver")
    login(view, oidc, sub="view", email="view@a.com")
    assert view.post(url, json={"action": "reboot", "confirm": "erp-app"}).status_code == 403
    ops = TestClient(app, base_url="http://testserver")
    login(ops, oidc, sub="ops", email="ops@a.com")
    _expire_mfa(ops)
    r = ops.post(url, json={"action": "reboot", "confirm": "erp-app"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "step_up_required"


def test_failed_action_is_not_retried(client, oidc, collected, monkeypatch, tmp_path):
    from app.collectors import aws_actions

    s = collected
    client.post(
        f"/api/tenants/{s['a']['id']}/resources/{s['res_a']['id']}/actions",
        json={"action": "reboot", "confirm": "erp-app"},
    )

    def boom(*a, **k):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(aws_actions, "account_session", boom)
    w = _worker(monkeypatch, tmp_path)
    w.run_once()
    with _db() as db:
        req = db.query(ActionRequest).one()
        job = db.query(Job).filter_by(kind="action").one()
        assert req.status == "failed" and job.status == "done" or job.status == "error"
        assert job.status != "queued"


# --- Agendador -----------------------------------------------------------------------------------


def test_scheduler_enqueues_once(client, oidc, setup, ctx):
    s = setup
    with _db() as db:
        first = scheduler.tick(db)
        db.commit()
    # Conexão ainda pendente: só tarefas de conexão, nada de inventário
    assert {k.split(":")[0] for k in first} == {
        "evaluate",
        "discover",
        "costs",
        "invoices",
        "aws_health",
        "optimization",
    }
    _run(ctx, "discover", s["conn"]["id"])
    with _db() as db:
        second = scheduler.tick(db)
        db.commit()
    assert {k.split(":")[0] for k in second} == {"inventory", "health"}
    with _db() as db:
        assert scheduler.tick(db) == []


def test_sync_now_and_jobs_api(client, oidc, setup):
    s = setup
    r = client.post(f"/api/admin/connections/{s['conn']['id']}/test")
    assert r.status_code == 202 and r.json()["kind"] == "discover"
    q = client.post(f"/api/admin/connections/{s['conn']['id']}/sync").json()["queued"]
    assert "discover" not in q  # já estava na fila
    jobs = client.get("/api/admin/jobs", params={"connection_id": s["conn"]["id"]}).json()
    assert {j["kind"] for j in jobs} >= {"discover", "costs"}


def test_hub_credentials_are_sealed(client, oidc, tmp_path):
    from app.main import app
    from app.vault import Vault, get_vault, keygen

    keygen(str(tmp_path))
    pub = (tmp_path / "vault-public.pem").read_bytes()
    app.dependency_overrides[get_vault] = lambda: Vault(pub)
    admin(client, oidc)
    body = {
        "access_key_id": "AKIA" + "A" * 16,
        "secret_access_key": "s" * 40,
        "principal_arn": "arn:aws:iam::999999999999:user/cloudview-hub",
    }
    r = client.put("/api/admin/aws/hub", json=body)
    assert r.status_code == 200 and r.json()["mode"] == "vault" and r.json()["access_key_hint"] == "…AAAA"
    from app.models import CloudSecret

    with _db() as db:
        sealed = db.query(CloudSecret).one().sealed
        assert "s" * 40 not in sealed and "AKIA" not in sealed
        # O worker (com a chave privada) monta a sessão hub a partir do cofre
        priv = (tmp_path / "vault-private.pem").read_bytes()
        hub = AwsHub.from_settings(db, Vault(pub, [priv]))
        assert hub.base.get_credentials().access_key == "AKIA" + "A" * 16
    assert client.delete("/api/admin/aws/hub").json()["mode"] == "default"
    with _db() as db:
        assert db.query(CloudSecret).count() == 0
