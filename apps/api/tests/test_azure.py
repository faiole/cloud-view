"""Fase 3: coleta e ações no Azure, contra o Azure falso de tests/fake_azure.py."""

import base64
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.azure.client import AzureCredential, AzureHub, client_assertion
from app.collectors import azure
from app.collectors.common import Ctx
from app.main import app
from app.models import CloudAccount, Connection, CostLine, HealthEvent, Recommendation
from app.services import scheduler
from app.services.storage import LocalStorage
from app.vault import Vault, get_vault, keygen
from tests import fake_azure as fa
from tests.helpers import admin, focus_csv, line
from tests.test_aws_collection import _db


@pytest.fixture
def fake():
    return fa.FakeAzure()


@pytest.fixture
def actx(fake, tmp_path):
    hub = AzureHub(
        AzureCredential(client_id="cid", client_secret="segredo-do-app"), transport=httpx.MockTransport(fake)
    )
    hub.sleep = lambda s: None
    return Ctx(hub=None, storage=LocalStorage(str(tmp_path / "docs")), azure=hub)


@pytest.fixture
def vault_keys(tmp_path):
    keygen(str(tmp_path / "v"))
    pub = (tmp_path / "v" / "vault-public.pem").read_bytes()
    priv = (tmp_path / "v" / "vault-private.pem").read_bytes()
    app.dependency_overrides[get_vault] = lambda: Vault(pub)
    return Vault(pub, [priv])


@pytest.fixture
def az(client, oidc):
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "Empresa A", "slug": "empresa-a"}).json()
    b = client.post("/api/tenants", json={"name": "Empresa B", "slug": "empresa-b"}).json()
    r = client.post(
        "/api/admin/connections/azure", json={"name": "Tenant Cliente", "tenant_id": fa.TENANT.upper()}
    )
    assert r.status_code == 201, r.text
    return {"a": a, "b": b, "conn": r.json()}


def _conn(db, s):
    return db.get(Connection, s["conn"]["id"])


def _acc(db, sub):
    return db.query(CloudAccount).filter_by(native_id=sub).one()


def _discover_and_assign(client, actx, s):
    with _db() as db:
        out = azure.discover(db, actx, _conn(db, s))
        db.commit()
    accs = {x["native_id"]: x for x in client.get("/api/admin/accounts").json()}
    client.post(f"/api/admin/accounts/{accs[fa.SUB_A]['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    client.post(
        f"/api/admin/accounts/{accs[fa.SUB_S]['id']}/assignments",
        json={"tenant_id": s["b"]["id"], "tag_key": "cost-center", "tag_value": "empresa-b"},
    )
    return out


def _inventory(actx, s):
    with _db() as db:
        for sub in (fa.SUB_A, fa.SUB_S):
            azure.collect_inventory(db, actx, _conn(db, s), _acc(db, sub))
        db.commit()


def test_discovery(client, az, actx, fake):
    out = _discover_and_assign(client, actx, az)
    assert out == {"subscriptions": 2, "new_subscriptions": [fa.SUB_A, fa.SUB_S]}
    conn = client.get("/api/admin/connections").json()[0]
    assert conn["status"] == "ok" and conn["accounts"] == 2 and conn["native_id"] == fa.TENANT
    # Token pedido no tenant do cliente com o app hub (client credentials)
    assert (
        fake.token_forms[0]["client_id"] == "cid" and fake.token_forms[0]["client_secret"] == "segredo-do-app"
    )
    assert ("POST", f"login.microsoftonline.com/{fa.TENANT}/oauth2/v2.0/token") in fake.calls


def test_inventory_network_and_findings(client, az, actx):
    _discover_and_assign(client, actx, az)
    _inventory(actx, az)
    a = {
        r["name"]: r
        for r in client.get(f"/api/tenants/{az['a']['id']}/resources", params={"type": "vm"}).json()
    }
    vm = a["erp-vm"]
    assert vm["instance_type"] == "Standard_D4s_v5" and vm["vcpu"] == 4 and vm["memory_mib"] == 16384
    assert vm["state"] == "running" and vm["private_ips"] == ["10.0.1.4"] and vm["public_ips"] == ["20.1.2.3"]
    assert vm["vpc_id"] == fa.VNET.lower() and vm["security_groups"][0]["name"] == "erp-nsg"
    codes = {f["code"] for f in vm["findings"]}
    assert "sg_open_22" in codes and "sg_open_3389" not in codes  # RDP só do escritório

    b = client.get(f"/api/tenants/{az['b']['id']}/resources", params={"type": "vm"}).json()
    assert [r["name"] for r in b] == ["site-b"]
    assert {f["code"] for f in b[0]["findings"]} == {"vm_stopped_not_deallocated"}

    others = {
        r["name"]: r
        for r in client.get(
            f"/api/tenants/{az['a']['id']}/resources", params={"with_findings": "true", "type": "disk"}
        ).json()
    }
    assert others["old-disk"]["findings"][0]["code"] == "disk_unattached"
    ips = {
        r["name"]: {f["code"] for f in r["findings"]}
        for r in client.get(f"/api/tenants/{az['a']['id']}/resources", params={"type": "public_ip"}).json()
    }
    assert ips["old-ip"] == {"public_ip_unassociated", "public_ip_basic_sku"}

    net = client.get(f"/api/tenants/{az['a']['id']}/network").json()
    assert (
        net["vpcs"][0]["vpc"]["name"] == "vnet-prod"
        and net["vpcs"][0]["subnets"][0]["attributes"]["cidr"] == "10.0.1.0/24"
    )
    assert [i["name"] for i in net["vpcs"][0]["exposed_instances"]] == ["erp-vm"]


def test_rule_recommendations_for_azure(client, az, actx):
    from app.services.rule_recommendations import sync_rule_recommendations

    _discover_and_assign(client, actx, az)
    _inventory(actx, az)
    with _db() as db:
        sync_rule_recommendations(db, _acc(db, fa.SUB_A))
        sync_rule_recommendations(db, _acc(db, fa.SUB_S))
        db.commit()
    recs = {
        r["title"].split(":")[0]: r
        for r in client.get(f"/api/tenants/{az['a']['id']}/recommendations").json()["items"]
    }
    assert Decimal(recs["Remover disco sem uso"]["monthly_savings"]) == Decimal("17.28")  # 128 GiB × 0,135
    assert "Liberar IP público sem uso" in recs
    b = client.get(f"/api/tenants/{az['b']['id']}/recommendations").json()["items"]
    assert b[0]["title"].startswith("Desalocar VM parada")


def test_health_and_service_events(client, az, actx, fake):
    _discover_and_assign(client, actx, az)
    _inventory(actx, az)
    fake.resource_health = "Unavailable"
    with _db() as db:
        out = azure.collect_health(db, actx, _conn(db, az), _acc(db, fa.SUB_A))
        db.commit()
    assert out["impaired"] == 1
    h = client.get(f"/api/tenants/{az['a']['id']}/health").json()
    assert [r["name"] for r in h["impaired"]] == ["erp-vm"]
    sources = {e["source"]: e for e in h["events"]}
    assert sources["azure_service_health"]["status"] == "upcoming"
    assert sources["azure_resource_health"]["title"].startswith("VM erp-vm")

    fake.resource_health = "Available"
    with _db() as db:
        azure.collect_health(db, actx, _conn(db, az), _acc(db, fa.SUB_A))
        db.commit()
        ev = db.query(HealthEvent).filter_by(source="azure_resource_health").one()
        assert ev.status == "closed"


def test_cost_management_with_retry_and_replace(client, az, actx, fake):
    _discover_and_assign(client, actx, az)
    with _db() as db:
        out = azure.collect_costs(db, actx, _conn(db, az), today=date(2026, 10, 20))
        fake.cost_rows["ActualCost"][0][0] = 120.0
        azure.collect_costs(db, actx, _conn(db, az), today=date(2026, 10, 20))
        db.commit()
    assert out == {"source": "azure_cost_management", "rows": {"2026-10": 1}}
    rep = client.get(
        f"/api/tenants/{az['a']['id']}/reports/usage", params={"start": "2026-10-01", "end": "2026-10-31"}
    ).json()
    assert Decimal(rep["totals"][0]["cost"]) == 120 and rep["rows"][0]["key"] == "Virtual Machines"
    eff = client.get(
        f"/api/tenants/{az['a']['id']}/reports/usage",
        params={"start": "2026-10-01", "end": "2026-10-31", "metric": "effective"},
    ).json()
    assert Decimal(eff["totals"][0]["cost"]) == 80
    with _db() as db:
        # A subscription continua ligada à conexão do tenant (inventário e saúde dependem disso)
        assert _acc(db, fa.SUB_A).connection_id == az["conn"]["id"]
        assert db.query(CostLine).one().region == "brazilsouth"


def test_focus_export_on_blob_uses_latest_run(client, az, actx, fake):
    r = client.patch(
        f"/api/admin/connections/{az['conn']['id']}",
        json={
            "cost_source": "focus_blob",
            "focus_s3_uri": "https://cvexports.blob.core.windows.net/exports/focus",
        },
    )
    assert r.status_code == 200
    _discover_and_assign(client, actx, az)

    def csv(cost):
        rows = [line("billing-acct-1", fa.SUB_S, 5, "Virtual Machines", cost, {"cost-center": "empresa-b"})]
        rows[0]["ChargePeriodStart"] = "2026-10-05T00:00:00Z"
        return focus_csv(rows).replace(b"AWS", b"Microsoft")

    base = "focus/cloudview-focus/20261001-20261031"
    fake.blobs[f"{base}/run-old/part_0_0001.csv.gz"] = (fa.gz(csv(10)), "Mon, 05 Oct 2026 10:00:00 GMT")
    fake.blobs[f"{base}/run-new/part_0_0001.csv.gz"] = (fa.gz(csv(55)), "Tue, 20 Oct 2026 10:00:00 GMT")
    fake.blobs[f"{base}/run-new/manifest.json"] = (b"{}", "Tue, 20 Oct 2026 10:00:00 GMT")
    with _db() as db:
        out = azure.collect_costs(db, actx, _conn(db, az), today=date(2026, 10, 20))
        db.commit()
    assert out["files"]["2026-10"] == 1
    rep = client.get(
        f"/api/tenants/{az['b']['id']}/reports/usage", params={"start": "2026-10-01", "end": "2026-10-31"}
    ).json()
    assert Decimal(rep["totals"][0]["cost"]) == 55
    with _db() as db:
        assert _acc(db, fa.SUB_S).connection_id == az["conn"]["id"]


def test_advisor_recommendations(client, az, actx, fake):
    _discover_and_assign(client, actx, az)
    _inventory(actx, az)
    with _db() as db:
        assert azure.collect_optimization(db, actx, _conn(db, az)) == {"advisor": {"recommendations": 1}}
        db.commit()
    recs = client.get(f"/api/tenants/{az['a']['id']}/recommendations").json()["items"]
    adv = next(r for r in recs if r["source"] == "azure_advisor")
    assert adv["action_type"] == "Rightsize" and Decimal(adv["monthly_savings"]) == Decimal("61.50")
    assert adv["current"] == "Standard_D8s_v5" and adv["recommended"] == "Standard_D4s_v5"
    fake.advisor = []
    with _db() as db:
        azure.collect_advisor(db, actx, _conn(db, az))
        db.commit()
        assert db.query(Recommendation).filter_by(source="azure_advisor").one().status == "resolved"


def test_vm_action_through_worker(client, az, actx, fake, monkeypatch, tmp_path):
    from app import worker as wmod

    _discover_and_assign(client, actx, az)
    _inventory(actx, az)
    vm = next(r for r in client.get(f"/api/tenants/{az['a']['id']}/resources", params={"type": "vm"}).json())
    r = client.post(
        f"/api/tenants/{az['a']['id']}/resources/{vm['id']}/actions",
        json={"action": "stop", "confirm": "erp-vm"},
    )
    assert r.status_code == 202, r.text

    w = wmod.Worker(only={"action"}, schedule=False)
    w._ctx = (datetime.now(UTC).timestamp() + 10_000, actx)
    assert w.run_once()
    act = client.get(f"/api/tenants/{az['a']['id']}/actions").json()[0]
    assert act["status"] == "succeeded", act["result"]
    assert act["result"]["state"] == "deallocated"
    assert (
        "POST",
        f"management.azure.com{fa.VM_ID.lower()}/deallocate",
    ) in fake.calls  # IDs do Azure não diferenciam caixa

    # VM de B sem a tag cloudview-actions
    b = client.get(f"/api/tenants/{az['b']['id']}/resources", params={"type": "vm"}).json()[0]
    r = client.post(
        f"/api/tenants/{az['b']['id']}/resources/{b['id']}/actions",
        json={"action": "start", "confirm": "site-b"},
    )
    assert r.status_code == 409


def test_invoices_for_exclusive_tenant(client, az, actx, fake):
    _discover_and_assign(client, actx, az)
    client.patch(
        f"/api/admin/connections/{az['conn']['id']}", json={"billing_account_id": "1234:5678_2019-05-31"}
    )
    with _db() as db:
        # Setembro: só a empresa A tem custo neste tenant
        db.add(
            CostLine(
                tenant_id=az["a"]["id"],
                cloud_account_id=_acc(db, fa.SUB_A).id,
                provider="azure",
                billing_account_id=fa.TENANT,
                charge_date=date(2026, 9, 3),
                service="VM",
                currency="USD",
                billed_cost=Decimal(1),
                effective_cost=Decimal(1),
                tags={},
            )
        )
        db.commit()
    fake.invoices = [
        {
            "name": "G012345678",
            "properties": {
                "invoicePeriodStartDate": "2026-09-01",
                "dueDate": "2026-10-30T00:00:00Z",
                "totalAmount": {"currency": "BRL", "value": 512.3},
            },
        }
    ]
    with _db() as db:
        out = azure.collect_invoices(db, actx, _conn(db, az), today=date(2026, 10, 20))
        db.commit()
    assert out["published"] == ["G012345678"]
    docs = client.get(f"/api/tenants/{az['a']['id']}/documents", params={"year": 2026}).json()
    assert docs[0]["issuer"] == "Microsoft" and Decimal(docs[0]["amount"]) == Decimal("512.3")


def test_client_assertion_is_valid_rs256(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloudview")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cred = AzureCredential(
        client_id="cid",
        certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
    )
    jwt = client_assertion(cred, "https://login.microsoftonline.com/t/oauth2/v2.0/token")
    h, p, sig = jwt.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    header, payload = (
        json.loads(base64.urlsafe_b64decode(pad(h))),
        json.loads(base64.urlsafe_b64decode(pad(p))),
    )
    assert header["alg"] == "RS256" and base64.urlsafe_b64decode(pad(header["x5t"])) == cert.fingerprint(
        hashes.SHA1()
    )  # noqa: S303
    assert payload["iss"] == payload["sub"] == "cid" and payload["aud"].endswith("/oauth2/v2.0/token")
    key.public_key().verify(
        base64.urlsafe_b64decode(pad(sig)), f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_nsg_rule_parsing():
    f = azure.nsg_findings(
        [
            {
                "name": "range",
                "properties": {
                    "direction": "Inbound",
                    "access": "Allow",
                    "sourceAddressPrefixes": ["0.0.0.0/0"],
                    "destinationPortRanges": ["3000-3400"],
                },
            },
            {
                "name": "deny",
                "properties": {
                    "direction": "Inbound",
                    "access": "Deny",
                    "sourceAddressPrefix": "*",
                    "destinationPortRange": "*",
                },
            },
            {
                "name": "out",
                "properties": {
                    "direction": "Outbound",
                    "access": "Allow",
                    "sourceAddressPrefix": "*",
                    "destinationPortRange": "*",
                },
            },
        ]
    )
    assert [x["code"] for x in f] == ["sg_open_3389", "sg_open_3306"] or {x["code"] for x in f} == {
        "sg_open_3389",
        "sg_open_3306",
    }


def test_azure_api_and_scheduler(client, oidc, az, vault_keys):
    s = az
    dup = client.post("/api/admin/connections/azure", json={"name": "x", "tenant_id": fa.TENANT})
    assert dup.status_code == 409
    bad = client.patch(f"/api/admin/connections/{s['conn']['id']}", json={"cost_source": "cost_explorer"})
    assert bad.status_code == 422
    own = client.post(
        "/api/admin/connections/azure",
        json={
            "name": "Cliente com app próprio",
            "tenant_id": "99999999-2222-3333-4444-555555555555",
            "own_app": {
                "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "client_secret": "segredo-proprio-123",
            },
        },
    )
    assert own.status_code == 201 and own.json()["client_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    hub = client.put(
        "/api/admin/azure/hub",
        json={
            "client_id": "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
            "home_tenant_id": fa.TENANT,
            "client_secret": "segredo-hub-123",
        },
    )
    assert hub.status_code == 200 and hub.json()["auth"] == "secret" and hub.json()["configured"]

    with _db() as db:
        # A credencial do app hub só abre no worker (chave privada do cofre)
        h = AzureHub.from_settings(db, vault_keys)
        assert h.credential.client_secret == "segredo-hub-123"
        own_conn = db.query(Connection).filter_by(native_id="99999999-2222-3333-4444-555555555555").one()
        assert h.credential_for(db, vault_keys, own_conn).client_secret == "segredo-proprio-123"
        kinds = {k.split(":")[0] for k in scheduler.tick(db)}
        db.commit()
    assert "aws_health" not in kinds and "invoices" not in kinds  # sem conta de cobrança informada
    assert {"discover", "costs", "optimization"} <= kinds


def test_token_cache(fake):
    hub = AzureHub(AzureCredential(client_id="cid", client_secret="s"), transport=httpx.MockTransport(fake))
    arm = hub.arm(fa.TENANT, hub.credential)
    arm.list_all("/subscriptions", "2022-12-01")
    arm.list_all("/subscriptions", "2022-12-01")
    assert len(fake.token_forms) == 1


def test_worker_context_loads_every_cloud(client, oidc, vault_keys):
    """Regressão: o contexto do worker real precisa trazer as credenciais de AWS, Azure e OCI."""
    from app import worker as wmod

    admin(client, oidc)
    client.put(
        "/api/admin/azure/hub",
        json={
            "client_id": "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
            "home_tenant_id": fa.TENANT,
            "client_secret": "segredo-hub-123",
        },
    )
    with _db() as db:
        ctx = wmod.build_ctx(db, vault_keys)
    assert ctx.hub is not None and ctx.oci is not None
    assert ctx.azure.credential.client_secret == "segredo-hub-123"
