"""Fase 2: recomendações, cobertura de Savings Plans, orçamentos, anomalias, alertas e notificações."""

import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal

import boto3
import pytest
from fastapi.testclient import TestClient

from app.collectors import aws_inventory, aws_optimization
from app.main import app
from app.models import Alert, Connection, NotificationDelivery, Recommendation, Resource
from app.services import anomalies, budgets, evaluate, notifications, notify
from app.services.alerts import channels_for, raise_alert
from app.vault import Vault, get_vault, keygen
from tests.conftest import login
from tests.helpers import admin, focus_csv, import_file, line
from tests.test_aws_collection import MOTO_ACCOUNT, _assign_by_tag, _db, _run, aws, ctx, setup  # noqa: F401


def _as(oidc, sub, email):
    c = TestClient(app, base_url="http://testserver")
    login(c, oidc, sub=sub, email=email)
    return c


@pytest.fixture
def vault_keys(tmp_path):
    keygen(str(tmp_path / "vault"))
    pub = (tmp_path / "vault" / "vault-public.pem").read_bytes()
    priv = (tmp_path / "vault" / "vault-private.pem").read_bytes()
    app.dependency_overrides[get_vault] = lambda: Vault(pub)
    return Vault(pub, [priv])


# --- Recomendações por regra (inventário) --------------------------------------------------------


def test_rule_recommendations_from_inventory(client, oidc, setup, ctx):
    from app.services.rule_recommendations import sync_rule_recommendations

    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == MOTO_ACCOUNT)
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    with _db() as db:
        from app.models import CloudAccount

        out = sync_rule_recommendations(db, db.query(CloudAccount).filter_by(native_id=MOTO_ACCOUNT).one())
        db.commit()
    assert out["rule_recommendations"] >= 3

    rec = client.get(f"/api/tenants/{s['a']['id']}/recommendations").json()
    titles = {r["title"].split(":")[0] for r in rec["items"]}
    assert {"Remover volume sem uso", "Liberar IP elástico sem uso", "Migrar volume gp2 para gp3"} <= titles
    vol = next(r for r in rec["items"] if r["title"].startswith("Remover volume"))
    assert Decimal(vol["monthly_savings"]) == Decimal("5.00")  # 50 GiB gp2 × US$ 0,10
    assert vol["estimated"] is True
    assert Decimal(rec["savings"][0]["cost"]) >= Decimal("8.65")

    # Volume removido na AWS → recomendação resolvida na próxima coleta
    ec2 = boto3.client("ec2", region_name="us-east-1")
    for v in ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]:
        ec2.delete_volume(VolumeId=v["VolumeId"])
    _run(ctx, "inventory", s["conn"]["id"], MOTO_ACCOUNT)
    with _db() as db:
        from app.models import CloudAccount

        sync_rule_recommendations(db, db.query(CloudAccount).filter_by(native_id=MOTO_ACCOUNT).one())
        db.commit()
    titles = {
        r["title"].split(":")[0]
        for r in client.get(f"/api/tenants/{s['a']['id']}/recommendations").json()["items"]
    }
    assert "Remover volume sem uso" not in titles


def test_recommendation_status_permissions(client, oidc, setup, ctx):
    s = setup
    with _db() as db:
        rec = Recommendation(
            tenant_id=s["a"]["id"],
            source="rules",
            native_id="x",
            action_type="Delete",
            title="Remover volume: v1",
            monthly_savings=Decimal("5"),
        )
        db.add(rec)
        db.commit()
        rid = rec.id
    for email, role in (("fin@a.com", "finance"), ("view@a.com", "viewer")):
        client.put(f"/api/tenants/{s['a']['id']}/members", json={"email": email, "role": role})
    view = _as(oidc, "v", "view@a.com")
    assert (
        view.patch(
            f"/api/tenants/{s['a']['id']}/recommendations/{rid}", json={"status": "dismissed"}
        ).status_code
        == 403
    )
    fin = _as(oidc, "f", "fin@a.com")
    r = fin.patch(
        f"/api/tenants/{s['a']['id']}/recommendations/{rid}",
        json={"status": "dismissed", "note": "volume de backup"},
    )
    assert r.status_code == 200 and r.json()["status_by"] == "fin@a.com"
    assert fin.get(f"/api/tenants/{s['a']['id']}/recommendations").json()["open"] == 0
    assert fin.get(f"/api/tenants/{s['b']['id']}/recommendations").status_code == 404


# --- Cost Optimization Hub e Savings Plans ------------------------------------------------------------


class FakeCOH:
    def __init__(self, items, error=None):
        self.items, self.error = items, error

    def list_recommendations(self, **kw):
        if self.error:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": self.error, "Message": "x"}}, "ListRecommendations")
        return {"items": self.items}


class FakeCE:
    def __init__(self, coverage=None, anomalies=None):
        self.coverage, self.anomalies = coverage or [], anomalies or []

    def get_savings_plans_coverage(self, **kw):
        return {"SavingsPlansCoverages": self.coverage}

    def get_anomalies(self, **kw):
        return {"Anomalies": self.anomalies}


def _coh_item(rid, tags, savings):
    return {
        "recommendationId": rid,
        "accountId": MOTO_ACCOUNT,
        "region": "us-east-1",
        "resourceId": f"i-{rid}",
        "currentResourceType": "Ec2Instance",
        "actionType": "Rightsize",
        "estimatedMonthlySavings": savings,
        "estimatedMonthlyCost": 100.0,
        "estimatedSavingsPercentage": 35.0,
        "currencyCode": "USD",
        "implementationEffort": "Medium",
        "restartNeeded": True,
        "currentResourceSummary": "m5.2xlarge",
        "recommendedResourceSummary": "m7g.xlarge",
        "tags": [{"key": k, "value": v} for k, v in tags.items()],
    }


def _patch_clients(monkeypatch, **fakes):
    real = aws_optimization.client
    monkeypatch.setattr(
        aws_optimization,
        "client",
        lambda sess, svc, region="us-east-1": fakes.get(svc) or real(sess, svc, region),
    )


def test_cost_optimization_hub_by_tag(client, oidc, setup, ctx, monkeypatch):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    _assign_by_tag(client, MOTO_ACCOUNT, [(s["a"]["id"], "empresa-a"), (s["b"]["id"], "empresa-b")])
    _patch_clients(
        monkeypatch,
        **{
            "cost-optimization-hub": FakeCOH(
                [
                    _coh_item("r1", {"cost-center": "empresa-a"}, 35.0),
                    _coh_item("r2", {"cost-center": "empresa-b"}, 50.0),
                ]
            )
        },
        ce=FakeCE(
            coverage=[
                {
                    "Attributes": {"LINKED_ACCOUNT": MOTO_ACCOUNT},
                    "Coverage": {
                        "SpendCoveredBySavingsPlans": "30",
                        "OnDemandCost": "70",
                        "TotalCost": "100",
                    },
                }
            ]
        ),
    )
    with _db() as db:
        out = aws_optimization.collect_optimization(db, ctx, db.get(Connection, s["conn"]["id"]))
        db.commit()
    assert out["recommendations"] == {"recommendations": 2, "resolved": 0}
    a = client.get(f"/api/tenants/{s['a']['id']}/recommendations").json()
    assert [r["recommended"] for r in a["items"]] == ["m7g.xlarge"]
    assert a["items"][0]["title"] == "Redimensionar: i-r1" and Decimal(a["items"][0]["monthly_savings"]) == 35
    # Conta dividida por tag: a cobertura de Savings Plans é da conta inteira e não vai para nenhuma empresa
    assert client.get(f"/api/tenants/{s['a']['id']}/commitments").json() == []

    _patch_clients(
        monkeypatch,
        **{"cost-optimization-hub": FakeCOH([_coh_item("r2", {"cost-center": "empresa-b"}, 50.0)])},
        ce=FakeCE(),
    )
    with _db() as db:
        out = aws_optimization.collect_recommendations(db, ctx, db.get(Connection, s["conn"]["id"]))
        db.commit()
    assert out["resolved"] == 1
    assert client.get(f"/api/tenants/{s['a']['id']}/recommendations").json()["open"] == 0


def test_sp_coverage_for_whole_account(client, oidc, setup, ctx, monkeypatch):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == MOTO_ACCOUNT)
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    cov = [
        {
            "Attributes": {"LINKED_ACCOUNT": MOTO_ACCOUNT},
            "Coverage": {"SpendCoveredBySavingsPlans": "30", "OnDemandCost": "70", "TotalCost": "100"},
        }
    ]
    _patch_clients(monkeypatch, ce=FakeCE(coverage=cov))
    with _db() as db:
        aws_optimization.collect_sp_coverage(
            db, ctx, db.get(Connection, s["conn"]["id"]), today=date(2026, 10, 20)
        )
        db.commit()
    rows = client.get(f"/api/tenants/{s['a']['id']}/commitments").json()
    assert rows == [
        {
            "month": "2026-10-01",
            "covered": "30.000000",
            "on_demand": "70.000000",
            "total": "100.000000",
            "coverage_pct": "30.0",
        }
    ]


def test_coh_not_enabled_is_reported(client, oidc, setup, ctx, monkeypatch):
    s = setup
    _patch_clients(
        monkeypatch, **{"cost-optimization-hub": FakeCOH([], error="OptInRequiredException")}, ce=FakeCE()
    )
    with _db() as db:
        out = aws_optimization.collect_optimization(db, ctx, db.get(Connection, s["conn"]["id"]))
    assert "não habilitado" in out["recommendations"]["skipped"]


def test_aws_anomalies_become_alerts(client, oidc, setup, ctx, monkeypatch):
    s = setup
    _run(ctx, "discover", s["conn"]["id"])
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == MOTO_ACCOUNT)
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    an = {
        "AnomalyId": "an-1",
        "AnomalyStartDate": "2026-10-18",
        "RootCauses": [{"Service": "Amazon EC2", "LinkedAccount": MOTO_ACCOUNT, "Region": "us-east-1"}],
        "Impact": {"TotalImpact": 250.0},
    }
    _patch_clients(monkeypatch, ce=FakeCE(anomalies=[an]))
    with _db() as db:
        conn = db.get(Connection, s["conn"]["id"])
        assert aws_optimization.collect_aws_anomalies(db, ctx, conn, today=date(2026, 10, 20)) == {
            "alerts": 1
        }
        assert aws_optimization.collect_aws_anomalies(db, ctx, conn, today=date(2026, 10, 20)) == {
            "alerts": 0
        }
        db.commit()
    alerts = client.get(f"/api/tenants/{s['a']['id']}/alerts").json()
    assert alerts[0]["type"] == "cost_anomaly_aws" and "250.00" in alerts[0]["message"]


# --- Orçamentos -----------------------------------------------------------------------------------


@pytest.fixture
def tenant_costs(client, oidc):
    """Empresa A com US$ 30/dia em setembro de 2026 (EC2 20 + S3 10)."""
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "A", "slug": "a"}).json()
    rows = []
    for d in range(1, 21):
        rows += [
            line("111111111111", "100000000001", d, "Amazon EC2", 20),
            line("111111111111", "100000000001", d, "Amazon S3", 10),
        ]
    import_file(client, focus_csv(rows))
    acc = client.get("/api/admin/accounts").json()[0]
    client.post(f"/api/admin/accounts/{acc['id']}/assignments", json={"tenant_id": a["id"]})
    client.put(f"/api/tenants/{a['id']}/members", json={"email": "fin@a.com", "role": "finance"})
    client.put(f"/api/tenants/{a['id']}/members", json={"email": "view@a.com", "role": "viewer"})
    return a


def test_budget_thresholds_and_forecast(client, oidc, tenant_costs):
    a = tenant_costs
    fin = _as(oidc, "f", "fin@a.com")
    r = fin.post(
        f"/api/tenants/{a['id']}/budgets",
        json={"name": "Mensal", "amount": "700", "thresholds": [50, 80, 100]},
    )
    assert r.status_code == 201
    view = _as(oidc, "v", "view@a.com")
    assert view.post(f"/api/tenants/{a['id']}/budgets", json={"name": "x", "amount": "1"}).status_code == 403

    with _db() as db:
        out = budgets.evaluate_budgets(db, today=date(2026, 9, 20))
        again = budgets.evaluate_budgets(db, today=date(2026, 9, 20))
        db.commit()
    # Realizado 600 (85,7%): avisos de 50% e 80%; previsão 900 > 700
    assert out == {"alerts": 3} and again == {"alerts": 0}
    b = fin.get(f"/api/tenants/{a['id']}/budgets").json()[0]
    assert (
        Decimal(b["actual"]) == 600 and Decimal(b["forecast"]) == 900 and Decimal(b["pct"]) == Decimal("85.7")
    )
    types = sorted((x["type"], x["severity"]) for x in fin.get(f"/api/tenants/{a['id']}/alerts").json())
    assert types == [("budget_actual", "info"), ("budget_actual", "warning"), ("budget_forecast", "warning")]


def test_budget_scope_by_service(client, oidc, tenant_costs):
    a = tenant_costs
    client.post(
        f"/api/tenants/{a['id']}/budgets",
        json={"name": "S3", "amount": "100", "services": ["Amazon S3"], "thresholds": [100]},
    )
    with _db() as db:
        budgets.evaluate_budgets(db, today=date(2026, 9, 20))
        db.commit()
    b = client.get(f"/api/tenants/{a['id']}/budgets").json()[0]
    assert Decimal(b["actual"]) == 200 and b["services"] == ["Amazon S3"]


# --- Anomalias ------------------------------------------------------------------------------------


def test_anomaly_detected_with_top_service(client, oidc, tenant_costs):
    a = tenant_costs
    import_file(
        client,
        focus_csv(
            [
                line("111111111111", "100000000001", 21, "Amazon EC2", 120),
                line("111111111111", "100000000001", 21, "Amazon S3", 10),
            ]
        ),
        "spike.csv",
    )
    with _db() as db:
        out = anomalies.detect_anomalies(db, today=date(2026, 9, 22))
        again = anomalies.detect_anomalies(db, today=date(2026, 9, 22))
        db.commit()
    assert out == {"alerts": 1} and again == {"alerts": 0}
    al = client.get(f"/api/tenants/{a['id']}/alerts").json()[0]
    assert al["type"] == "cost_anomaly" and al["severity"] == "critical"  # 130 vs 30: +333%
    assert "Amazon EC2 (+USD 100.00)" in al["message"]


def test_steady_costs_have_no_anomaly(client, oidc, tenant_costs):
    with _db() as db:
        assert anomalies.detect_anomalies(db, today=date(2026, 9, 21)) == {"alerts": 0}


# --- Alertas: visibilidade e status --------------------------------------------------------------


def test_alert_visibility_by_role(client, oidc, tenant_costs):
    a = tenant_costs
    with _db() as db:
        raise_alert(
            db,
            tenant_id=a["id"],
            type="cost_anomaly",
            category="cost",
            severity="warning",
            title="c",
            message="m",
            dedupe_key="k1",
        )
        raise_alert(
            db,
            tenant_id=a["id"],
            type="exposed_instance",
            category="security",
            severity="critical",
            title="s",
            message="m",
            dedupe_key="k2",
        )
        db.commit()
    fin = _as(oidc, "f", "fin@a.com")
    assert [x["category"] for x in fin.get(f"/api/tenants/{a['id']}/alerts").json()] == ["cost"]
    view = _as(oidc, "v", "view@a.com")
    cats = sorted(x["category"] for x in view.get(f"/api/tenants/{a['id']}/alerts").json())
    assert cats == ["cost", "security"]
    alert_id = next(
        x["id"] for x in view.get(f"/api/tenants/{a['id']}/alerts").json() if x["category"] == "cost"
    )
    assert view.post(f"/api/tenants/{a['id']}/alerts/{alert_id}/ack").status_code == 403
    assert fin.post(f"/api/tenants/{a['id']}/alerts/{alert_id}/ack").json()["status"] == "acknowledged"
    assert {x["title"] for x in client.get("/api/admin/alerts").json()} == {"c", "s"}


def test_resolved_alert_reopens_when_condition_returns(client, oidc, tenant_costs):
    a = tenant_costs
    with _db() as db:
        first = raise_alert(
            db,
            tenant_id=a["id"],
            type="t",
            category="cost",
            severity="warning",
            title="x",
            message="m",
            dedupe_key="k",
        )
        from app.services.alerts import resolve_alert

        assert resolve_alert(db, "k")
        again = raise_alert(
            db,
            tenant_id=a["id"],
            type="t",
            category="cost",
            severity="warning",
            title="x",
            message="m",
            dedupe_key="k",
        )
        db.commit()
        assert again is not None and again.id == first.id and again.status == "open"


def test_security_and_patch_alerts(client, oidc, tenant_costs):
    a = tenant_costs
    with _db() as db:
        from app.models import CloudAccount

        acc = db.query(CloudAccount).one()
        r = Resource(
            tenant_id=a["id"],
            cloud_account_id=acc.id,
            provider="aws",
            region="us-east-1",
            type="ec2_instance",
            native_id="i-1",
            name="erp",
            public_ips=["54.1.2.3"],
            findings=[
                {
                    "code": "sg_open_22",
                    "severity": "critical",
                    "message": "SSH (porta 22) aberto para a internet",
                },
                {
                    "code": "patches_missing_critical",
                    "severity": "critical",
                    "message": "2 atualizações críticas",
                },
            ],
            attributes={"patch": {"critical": 2, "security": 1}},
        )
        db.add(r)
        db.flush()
        assert evaluate.security_alerts(db) == 2
        r.findings = []
        evaluate.security_alerts(db)
        db.commit()
        assert {x.status for x in db.query(Alert).all()} == {"resolved"}


def test_connection_error_is_platform_alert(client, oidc):
    admin(client, oidc)
    with _db() as db:
        db.add(
            Connection(
                provider="aws",
                kind="aws_account",
                native_id="1",
                name="X",
                status="error",
                last_error="AccessDenied",
            )
        )
        db.flush()
        assert evaluate.platform_alerts(db) == 1
        db.commit()
    al = client.get("/api/admin/alerts").json()
    assert al[0]["tenant_id"] is None and al[0]["category"] == "platform"


def test_patch_collection_marks_findings():
    class FakeSSM:
        def get_paginator(self, op):
            data = {
                "describe_instance_information": {"InstanceInformationList": [{"InstanceId": "i-managed"}]},
                "describe_instance_patch_states": {
                    "InstancePatchStates": [
                        {
                            "InstanceId": "i-managed",
                            "CriticalNonCompliantCount": 3,
                            "SecurityNonCompliantCount": 1,
                            "MissingCount": 7,
                        }
                    ]
                },
            }[op]

            class P:
                def paginate(self, **_):
                    return [data]

            return P()

    class Up:
        pass

    managed = Resource(
        native_id="i-managed", type="ec2_instance", state="running", findings=[], attributes={}
    )
    unmanaged = Resource(native_id="i-free", type="ec2_instance", state="running", findings=[], attributes={})
    up = Up()
    up.by_key = {
        ("us-east-1", "ec2_instance", "i-managed"): managed,
        ("us-east-1", "ec2_instance", "i-free"): unmanaged,
    }
    up.seen = set(up.by_key)
    aws_inventory._collect_patches(FakeSSM(), "us-east-1", up)
    assert managed.attributes["patch"]["critical"] == 3
    assert {f["code"] for f in managed.findings} == {"patches_missing_critical", "patches_missing_security"}
    assert [f["code"] for f in unmanaged.findings] == ["ssm_unmanaged"]


# --- Notificações ---------------------------------------------------------------------------------


def test_channel_url_validation(client, oidc, tenant_costs, vault_keys, monkeypatch):
    a = tenant_costs
    url = f"/api/tenants/{a['id']}/channels"
    assert (
        client.post(url, json={"type": "slack", "name": "s", "url": "https://evil.example/hook"}).status_code
        == 422
    )
    assert (
        client.post(url, json={"type": "teams", "name": "t", "url": "https://hooks.slack.com/x"}).status_code
        == 422
    )
    assert (
        client.post(url, json={"type": "webhook", "name": "w", "url": "http://example.com"}).status_code
        == 422
    )
    monkeypatch.setattr(notify, "_resolves_public", lambda host: host != "internal.local")
    assert (
        client.post(url, json={"type": "webhook", "name": "w", "url": "https://internal.local/x"}).status_code
        == 422
    )
    r = client.post(
        url,
        json={
            "type": "slack",
            "name": "Slack financeiro",
            "url": "https://hooks.slack.com/services/T/B/secret",
        },
    )
    assert r.status_code == 201 and r.json()["target_hint"] == "hooks.slack.com"
    with _db() as db:
        from app.models import CloudSecret

        assert "hooks.slack.com/services/T/B/secret" not in db.query(CloudSecret).one().sealed
    fin = _as(oidc, "f", "fin@a.com")
    assert fin.get(url).status_code == 403  # canais: só administrador da empresa ou da plataforma


def test_alert_delivery_to_matching_channels(client, oidc, tenant_costs, vault_keys, monkeypatch):
    a = tenant_costs
    monkeypatch.setattr(notify, "_resolves_public", lambda host: True)
    posted = []
    monkeypatch.setattr(
        notify, "_post", lambda url, body, headers: posted.append((url, json.loads(body), headers))
    )
    sent_mail = []
    monkeypatch.setattr(notify, "send_email", lambda ch, p: sent_mail.append((ch.emails, p["title"])))

    client.post(
        f"/api/tenants/{a['id']}/channels",
        json={
            "type": "slack",
            "name": "slack",
            "url": "https://hooks.slack.com/services/x",
            "min_severity": "critical",
        },
    )
    hook = client.post(
        f"/api/tenants/{a['id']}/channels",
        json={
            "type": "webhook",
            "name": "hook",
            "url": "https://hooks.example.com/cv",
            "categories": ["cost"],
        },
    ).json()
    client.post(
        f"/api/tenants/{a['id']}/channels",
        json={"type": "email", "name": "mail", "emails": ["fin@a.com"], "categories": ["security"]},
    )
    client.post(
        "/api/admin/channels",
        json={
            "type": "teams",
            "name": "noc",
            "url": "https://x.webhook.office.com/abc",
            "categories": ["cost", "platform"],
            "min_severity": "info",
        },
    )

    with _db() as db:
        al = raise_alert(
            db,
            tenant_id=a["id"],
            type="budget_actual",
            category="cost",
            severity="warning",
            title="Orçamento 80%",
            message="m",
            dedupe_key="b1",
        )
        assert sorted(c.name for c in channels_for(db, al)) == ["hook", "noc"]
        out = notifications.process(db, vault_keys, {"alert_id": al.id})
        again = notifications.process(db, vault_keys, {"alert_id": al.id})
        db.commit()
    assert sorted(out["sent"]) == ["hook", "noc"] and again["sent"] == []
    assert sent_mail == []

    url, body, headers = next(p for p in posted if "hooks.example.com" in p[0])
    raw = json.dumps(body).encode()
    expected = (
        "sha256="
        + hmac.new(
            hook["signing_secret"].encode(),
            f"{headers['X-CloudView-Timestamp']}.".encode() + raw,
            hashlib.sha256,
        ).hexdigest()
    )
    assert headers["X-CloudView-Signature"] == expected and body["tenant"] == "A"
    teams = next(p for p in posted if "office.com" in p[0])[1]
    assert teams["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_failed_delivery_retries_job(client, oidc, tenant_costs, vault_keys, monkeypatch):
    a = tenant_costs

    def down(*a, **k):
        raise RuntimeError("destino respondeu 500")

    monkeypatch.setattr(notify, "_post", down)
    client.post(
        f"/api/tenants/{a['id']}/channels",
        json={"type": "slack", "name": "slack", "url": "https://hooks.slack.com/services/x"},
    )
    with _db() as db:
        al = raise_alert(
            db,
            tenant_id=a["id"],
            type="t",
            category="cost",
            severity="critical",
            title="x",
            message="m",
            dedupe_key="z",
        )
        with pytest.raises(RuntimeError):
            notifications.process(db, vault_keys, {"alert_id": al.id})
        db.commit()
        assert db.query(NotificationDelivery).one().status == "failed"


def test_test_notification(client, oidc, tenant_costs, vault_keys, monkeypatch):
    a = tenant_costs
    posted = []
    monkeypatch.setattr(notify, "_post", lambda url, body, headers: posted.append(json.loads(body)))
    ch = client.post(
        f"/api/tenants/{a['id']}/channels",
        json={"type": "slack", "name": "s", "url": "https://hooks.slack.com/services/x"},
    ).json()
    assert client.post(f"/api/tenants/{a['id']}/channels/{ch['id']}/test").status_code == 202
    with _db() as db:
        out = notifications.process(db, vault_keys, {"test": True, "channel_id": ch["id"]})
        db.commit()
    assert out == {"test": "sent"} and "Teste do Cloud View" in posted[0]["text"]
    assert client.get(f"/api/tenants/{a['id']}/channels").json()[0]["last_delivery"] == "ok"


def test_evaluate_all_runs(client, oidc, tenant_costs):
    with _db() as db:
        out = evaluate.evaluate_all(db, today=date(2026, 9, 20))
    assert set(out) == {"budgets", "anomalies", "health", "security", "platform"}
