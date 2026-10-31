"""Fase 5: topologia de rede, agenda liga/desliga e relatório mensal em PDF."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ActionRequest, CloudAccount, Document, Job, PowerSchedule, Recommendation, Resource
from app.services import monthly_jobs, notify
from app.services.monthly_report import render_pdf, report_data
from app.services.power_schedules import run_schedules, weekly_hours_off
from app.services.storage import LocalStorage
from tests.conftest import login
from tests.helpers import admin, focus_csv, import_file, line
from tests.test_aws_collection import _db


def _as(oidc, sub, email):
    c = TestClient(app, base_url="http://testserver")
    login(c, oidc, sub=sub, email=email)
    return c


@pytest.fixture
def world(client, oidc):
    """Empresa A com uma conta (custos de setembro/2026), rede e 3 instâncias; empresa B vazia."""
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "Empresa A", "slug": "empresa-a"}).json()
    b = client.post("/api/tenants", json={"name": "Empresa B", "slug": "empresa-b"}).json()
    rows = []
    for d in range(1, 31):
        rows += [
            line("111111111111", "100000000001", d, "Amazon EC2", 20, {"user:projeto": "erp"}),
            line("111111111111", "100000000001", d, "Amazon S3", 5),
        ]
    import_file(client, focus_csv(rows))
    acc = client.get("/api/admin/accounts").json()[0]
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": a["id"]})
    for email, role in (("fin@a.com", "finance"), ("view@a.com", "viewer"), ("ops@a.com", "operator")):
        client.put(f"/api/tenants/{a['id']}/members", json={"email": email, "role": role})
    with _db() as db:
        acc_id = db.query(CloudAccount).one().id

        def res(**kw):
            r = Resource(tenant_id=a["id"], cloud_account_id=acc_id, provider="aws", region="sa-east-1", **kw)
            db.add(r)
            return r

        res(
            type="vpc", native_id="vpc-1", name="vpc-prod", vpc_id="vpc-1", attributes={"cidr": "10.0.0.0/16"}
        )
        res(
            type="subnet",
            native_id="subnet-pub",
            name="publica",
            vpc_id="vpc-1",
            subnet_id="subnet-pub",
            attributes={"cidr": "10.0.1.0/24", "public_ip_on_launch": True},
        )
        res(
            type="subnet",
            native_id="subnet-priv",
            name="privada",
            vpc_id="vpc-1",
            subnet_id="subnet-priv",
            attributes={"cidr": "10.0.2.0/24"},
        )
        tags_on = {"cloudview-actions": "enabled", "agenda": "comercial"}
        res(
            type="ec2_instance",
            native_id="i-web",
            name="web",
            state="running",
            vpc_id="vpc-1",
            subnet_id="subnet-pub",
            private_ips=["10.0.1.5"],
            public_ips=["54.1.1.1"],
            tags=tags_on,
            findings=[
                {
                    "code": "sg_open_22",
                    "severity": "critical",
                    "message": "SSH (porta 22) aberto para a internet",
                }
            ],
        )
        res(
            type="ec2_instance",
            native_id="i-app",
            name="app",
            state="stopped",
            vpc_id="vpc-1",
            subnet_id="subnet-priv",
            private_ips=["10.0.2.7"],
            tags=tags_on,
        )
        res(
            type="ec2_instance",
            native_id="i-db",
            name="db",
            state="stopped",
            vpc_id="vpc-1",
            subnet_id="subnet-priv",
            private_ips=["10.0.2.9"],
            tags={"agenda": "comercial"},
        )  # sem a tag de ações: a agenda não mexe
        db.add(
            Recommendation(
                tenant_id=a["id"],
                source="rules",
                native_id="r1",
                action_type="Delete",
                title="Remover volume sem uso: vol-1",
                monthly_savings=Decimal("12.50"),
            )
        )
        db.commit()
    return {"a": a, "b": b}


# --- Topologia -----------------------------------------------------------------------------------


def test_topology(client, oidc, world):
    a, b = world["a"], world["b"]
    t = client.get(f"/api/tenants/{a['id']}/topology").json()
    assert t["exposed"] == 1 and t["unplaced"] == []
    net = t["networks"][0]
    assert net["name"] == "vpc-prod" and net["cidr"] == "10.0.0.0/16"
    assert [s["name"] for s in net["subnets"]] == ["publica", "privada"]  # públicas primeiro
    web = net["subnets"][0]["instances"][0]
    assert web["exposed"] and web["exposure"] == ["SSH (porta 22) aberto para a internet"]
    assert sorted(i["name"] for i in net["subnets"][1]["instances"]) == ["app", "db"]
    assert client.get(f"/api/tenants/{b['id']}/topology").json()["networks"] == []
    fin = _as(oidc, "f", "fin@a.com")
    assert fin.get(f"/api/tenants/{a['id']}/topology").status_code == 403


# --- Agenda liga/desliga ---------------------------------------------------------------------------

SCHED = {
    "name": "Horário comercial",
    "timezone": "America/Sao_Paulo",
    "days": [0, 1, 2, 3, 4],
    "start_time": "08:00",
    "stop_time": "20:00",
    "selector_tag": "agenda=comercial",
}


def test_schedule_permissions_and_validation(client, oidc, world):
    a = world["a"]
    url = f"/api/tenants/{a['id']}/schedules"
    view = _as(oidc, "v", "view@a.com")
    assert view.post(url, json=SCHED).status_code == 403
    assert client.post(url, json={**SCHED, "start_time": "21:00"}).status_code == 422
    assert client.post(url, json={**SCHED, "timezone": "Marte/Base"}).status_code == 422
    assert client.post(url, json={**SCHED, "start_time": None, "stop_time": None}).status_code == 422
    assert client.post(url, json={**SCHED, "selector_tag": None}).status_code == 422
    assert (
        client.post(url, json={**SCHED, "selector_tag": None, "resource_ids": ["outro-cliente"]}).status_code
        == 422
    )
    ops = _as(oidc, "o", "ops@a.com")
    r = ops.post(url, json=SCHED)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["weekly_hours_off"] == 108.0  # 168 h − 12 h × 5 dias
    assert sorted(t["name"] for t in s["targets"]) == ["app", "web"]  # "db" não tem a tag de ações
    assert view.get(url).json()[0]["name"] == "Horário comercial"


def test_schedule_runs_in_local_time_once_per_day(client, oidc, world):
    a = world["a"]
    client.post(f"/api/tenants/{a['id']}/schedules", json=SCHED)
    monday_0759_sp = datetime(2026, 11, 2, 10, 59, tzinfo=UTC)  # 07:59 em São Paulo (UTC−3)
    monday_0800_sp = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)
    monday_2005_sp = datetime(2026, 11, 2, 23, 5, tzinfo=UTC)
    saturday_0800_sp = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)
    with _db() as db:
        assert run_schedules(db, monday_0759_sp) == []
        assert run_schedules(db, monday_0800_sp) == ["start:app"]  # só a parada; "web" já está ligada
        assert run_schedules(db, monday_0800_sp) == []  # não repete no mesmo dia
        assert run_schedules(db, saturday_0800_sp) == []
        # "app" ainda tem o pedido de ligar em andamento; "web" está ligada → desliga
        assert run_schedules(db, monday_2005_sp) == ["stop:web"]
        db.commit()
        reqs = db.query(ActionRequest).order_by(ActionRequest.requested_at).all()
        assert [(r.action, r.requested_by) for r in reqs] == [
            ("start", "agenda: Horário comercial"),
            ("stop", "agenda: Horário comercial"),
        ]
        assert db.query(Job).filter_by(kind="action").count() == 2
        s = db.query(PowerSchedule).one()
        assert s.last_start_on == date(2026, 11, 2) and s.last_stop_on == date(2026, 11, 2)
    actions = client.get(f"/api/tenants/{a['id']}/actions").json()
    assert {x["reason"] for x in actions} == {"Agenda 'Horário comercial'"}


def test_weekly_hours_off_only_stop():
    s = PowerSchedule(name="x", days=[0], start_time=None, stop_time="19:00")
    assert weekly_hours_off(s) is None


# --- Relatório mensal ---------------------------------------------------------------------------------


def test_monthly_report_data_and_pdf(client, oidc, world):
    a = world["a"]
    client.post(f"/api/tenants/{a['id']}/budgets", json={"name": "Mensal", "amount": "700"})
    with _db() as db:
        from app.models import Tenant

        data = report_data(db, db.get(Tenant, a["id"]), date(2026, 9, 1))
        pdf = render_pdf(data)
    assert data["totals"] == {"USD": Decimal(750)}
    assert data["previous"] == {}
    assert [r["key"] for r in data["by_service"]] == ["Amazon EC2", "Amazon S3"]
    assert data["tag_key"] == "projeto" and data["by_tag"][0]["key"] == "erp"
    assert data["budgets"][0]["actual"] == Decimal(750) and data["budgets"][0]["pct"] > 100
    assert data["recommendations_open"] == 1 and data["savings"] == {"USD": Decimal("12.50")}
    assert data["instances"] == 3 and data["exposed"] == 1
    assert [m["month"] for m in data["trend"]][-1] == date(2026, 9, 1) and len(data["trend"]) == 6
    assert pdf.startswith(b"%PDF") and b"/Type /Page" in pdf and len(pdf) > 3000


def test_monthly_pdf_endpoint(client, oidc, world):
    a = world["a"]
    fin = _as(oidc, "f", "fin@a.com")
    r = fin.get(f"/api/tenants/{a['id']}/reports/monthly.pdf", params={"month": "2026-09"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert 'filename="relatorio-empresa-a-2026-09.pdf"' in r.headers["content-disposition"]
    assert fin.get(f"/api/tenants/{world['b']['id']}/reports/monthly.pdf").status_code == 404
    assert any(e["action"] == "report.monthly_pdf" for e in client.get("/api/audit").json())


def test_monthly_job_archives_and_emails(client, oidc, world, tmp_path, monkeypatch):
    a = world["a"]
    client.post(
        f"/api/tenants/{a['id']}/channels",
        json={"type": "email", "name": "fin", "emails": ["fin@a.com"], "categories": ["cost"]},
    )
    client.post(
        f"/api/tenants/{a['id']}/channels",
        json={"type": "email", "name": "noc", "emails": ["noc@a.com"], "categories": ["health"]},
    )
    sent = []
    monkeypatch.setattr(
        notify,
        "send_email",
        lambda ch, p, attachments=None: sent.append((ch.emails, p["title"], attachments)),
    )
    storage = LocalStorage(str(tmp_path / "docs"))
    with _db() as db:
        assert "skipped" in monthly_jobs.generate_monthly_reports(db, storage, today=date(2026, 10, 2))
        out = monthly_jobs.generate_monthly_reports(db, storage, today=date(2026, 10, 3))
        again = monthly_jobs.generate_monthly_reports(db, storage, today=date(2026, 10, 4))
        db.commit()
        doc = db.query(Document).one()
    assert out == {"month": "2026-09", "generated": ["Empresa A"], "emailed": 1, "email_failed": []}
    assert again["generated"] == []
    assert doc.kind == "report" and doc.number == "RM-2026-09" and doc.amount == Decimal(750)
    ((emails, title, attachments),) = sent
    assert emails == ["fin@a.com"] and title == "Relatório de custos de setembro de 2026"
    assert attachments[0][0] == "relatorio-empresa-a-2026-09.pdf" and attachments[0][1].startswith(b"%PDF")

    # Leitura vê o relatório mensal, mas não as faturas
    view = _as(oidc, "v", "view@a.com")
    docs = view.get(f"/api/tenants/{a['id']}/documents", params={"year": 2026}).json()
    assert [d["kind"] for d in docs] == ["report"]


def test_scheduler_runs_schedules_each_tick(client, oidc, world):
    from app.services import scheduler

    client.post(f"/api/tenants/{world['a']['id']}/schedules", json=SCHED)
    with _db() as db:
        created = scheduler.tick(db, datetime(2026, 11, 2, 11, 1, tzinfo=UTC))
        db.commit()
    assert "schedule:start:app" in created and "monthly_reports" in created
