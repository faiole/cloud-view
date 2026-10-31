"""Fase 4: coleta e ações na OCI, contra a OCI falsa de tests/fake_oci.py (que valida as assinaturas)."""

import gzip
import re
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.collectors import oci
from app.collectors.common import Ctx
from app.main import app
from app.models import CloudAccount, Connection
from app.oci.client import OciCredential, OciHub, fingerprint, generate_api_key
from app.services import scheduler
from app.services.storage import LocalStorage
from app.vault import Vault, get_vault, keygen
from tests import fake_oci as fo
from tests.helpers import admin, focus_csv, line
from tests.test_aws_collection import _db

USER = "ocid1.user.oc1..aaaaaaaacloudviewreader00000000000000000"


@pytest.fixture
def vault_keys(tmp_path):
    keygen(str(tmp_path / "v"))
    pub = (tmp_path / "v" / "vault-public.pem").read_bytes()
    priv = (tmp_path / "v" / "vault-private.pem").read_bytes()
    app.dependency_overrides[get_vault] = lambda: Vault(pub)
    return Vault(pub, [priv])


@pytest.fixture
def fake():
    return fo.FakeOci()


@pytest.fixture
def octx(fake, vault_keys, tmp_path):
    hub = OciHub(None, transport=httpx.MockTransport(fake))
    hub.sleep = lambda s: None
    return Ctx(hub=None, storage=LocalStorage(str(tmp_path / "docs")), vault=vault_keys, oci=hub)


@pytest.fixture
def oc(client, oidc, fake, vault_keys):
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "Empresa A", "slug": "empresa-a"}).json()
    b = client.post("/api/tenants", json={"name": "Empresa B", "slug": "empresa-b"}).json()
    r = client.post(
        "/api/admin/connections/oci",
        json={"name": "Cliente OCI", "tenancy_id": fo.TENANCY, "home_region": fo.REGION},
    )
    assert r.status_code == 201, r.text
    conn = r.json()
    # O cliente cola a chave pública no usuário dele e informa o OCID do usuário
    fake.register(f"{fo.TENANCY}/{USER}/{conn['oci_fingerprint']}", conn["oci_public_key"])
    assert client.patch(f"/api/admin/connections/{conn['id']}", json={"oci_user_id": USER}).status_code == 200
    return {"a": a, "b": b, "conn": conn}


def _conn(db, s):
    return db.get(Connection, s["conn"]["id"])


def _acc(db, native):
    return db.query(CloudAccount).filter_by(native_id=native).one()


def _setup(client, octx, s):
    with _db() as db:
        out = oci.discover(db, octx, _conn(db, s))
        db.commit()
    accs = {x["native_id"]: x for x in client.get("/api/admin/accounts").json()}
    client.post(f"/api/admin/accounts/{accs[fo.COMP_A]['id']}/assignments", json={"tenant_id": s["a"]["id"]})
    client.post(
        f"/api/admin/accounts/{accs[fo.COMP_S]['id']}/assignments",
        json={"tenant_id": s["b"]["id"], "tag_key": "cost-center", "tag_value": "empresa-b"},
    )
    return out


def _inventory(octx, s):
    with _db() as db:
        for comp in (fo.COMP_A, fo.COMP_S):
            oci.collect_inventory(db, octx, _conn(db, s), _acc(db, comp))
        db.commit()


def test_key_generation_and_private_key_sealed(oc, vault_keys):
    conn = oc["conn"]
    assert conn["oci_public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert re.fullmatch(r"([0-9a-f]{2}:){15}[0-9a-f]{2}", conn["oci_fingerprint"])
    with _db() as db:
        from app.models import CloudSecret

        sealed = db.query(CloudSecret).one().sealed
        assert "PRIVATE KEY" not in sealed
        cred = OciHub(None).credential_for(db, vault_keys, _conn(db, oc))
        assert cred.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----") and cred.user_id == USER


def test_discovery_compartments_with_paging(client, oc, octx):
    out = _setup(client, octx, oc)
    assert out["compartments"] == 3 and out["regions"] == [fo.REGION]  # raiz + 2 (duas páginas)
    names = {a["native_id"]: a["name"] for a in client.get("/api/admin/accounts").json()}
    assert names[fo.TENANCY] == "cliente (raiz)" and names[fo.COMP_S] == "compartilhado"
    assert client.get("/api/admin/connections").json()[0]["status"] == "ok"


def test_unsigned_or_wrong_key_is_rejected(fake):
    priv, pub, fp = generate_api_key()
    hub = OciHub(OciCredential(fo.TENANCY, USER, fp, priv), transport=httpx.MockTransport(fake))
    with pytest.raises(Exception, match="401"):
        hub.client(hub.credential).get("identity", fo.REGION, f"/tenancies/{fo.TENANCY}")
    fake.register(f"{fo.TENANCY}/{USER}/{fp}", pub)
    assert (
        hub.client(hub.credential).get("identity", fo.REGION, f"/tenancies/{fo.TENANCY}")["name"] == "cliente"
    )


def test_inventory_network_and_findings(client, oc, octx):
    _setup(client, octx, oc)
    _inventory(octx, oc)
    a = {
        r["name"]: r
        for r in client.get(f"/api/tenants/{oc['a']['id']}/resources", params={"type": "oci_instance"}).json()
    }
    inst = a["erp-app"]
    assert (
        inst["instance_type"] == "VM.Standard.E4.Flex" and inst["vcpu"] == 4 and inst["memory_mib"] == 32768
    )
    assert (
        inst["private_ips"] == ["10.0.1.10"]
        and inst["public_ips"] == ["150.1.2.3"]
        and inst["vpc_id"] == fo.VCN
    )
    codes = {f["code"] for f in inst["findings"]}
    assert {"sg_open_22", "sg_open_3389"} <= codes  # SSH pela security list da subnet, RDP pelo NSG
    assert inst["attributes"]["actions_enabled"] is True

    b = client.get(f"/api/tenants/{oc['b']['id']}/resources", params={"type": "oci_instance"}).json()
    assert [r["name"] for r in b] == ["site-b"] and b[0]["tags"]["Operations.owner"] == "time-b"
    assert {f["code"] for f in b[0]["findings"]} == {"previous_generation"}

    vols = {
        r["name"]: r
        for r in client.get(f"/api/tenants/{oc['a']['id']}/resources", params={"type": "block_volume"}).json()
    }
    assert [f["code"] for f in vols["old"]["findings"]] == ["block_volume_unattached"] and vols["data"][
        "findings"
    ] == []
    net = client.get(f"/api/tenants/{oc['a']['id']}/network").json()
    assert net["vpcs"][0]["vpc"]["name"] == "vcn-prod" and [
        i["name"] for i in net["vpcs"][0]["exposed_instances"]
    ] == ["erp-app"]


def test_rule_recommendation_for_block_volume(client, oc, octx):
    from app.services.rule_recommendations import sync_rule_recommendations

    _setup(client, octx, oc)
    _inventory(octx, oc)
    with _db() as db:
        sync_rule_recommendations(db, _acc(db, fo.COMP_A))
        db.commit()
    recs = client.get(f"/api/tenants/{oc['a']['id']}/recommendations").json()["items"]
    vol = next(r for r in recs if r["title"].startswith("Remover volume"))
    assert Decimal(vol["monthly_savings"]) == Decimal("5.10")  # 200 GB × US$ 0,0255


def test_maintenance_and_announcements(client, oc, octx):
    _setup(client, octx, oc)
    _inventory(octx, oc)
    accs = {x["native_id"]: x for x in client.get("/api/admin/accounts").json()}
    client.post(
        f"/api/admin/accounts/{accs[fo.TENANCY]['id']}/assignments", json={"tenant_id": oc["a"]["id"]}
    )
    with _db() as db:
        out = oci.collect_health(db, octx, _conn(db, oc), _acc(db, fo.COMP_A))
        out_root = oci.collect_health(db, octx, _conn(db, oc), _acc(db, fo.TENANCY))
        db.commit()
    assert out["maintenance"] == 1 and out_root["announcements"] == 1
    ev = {e["source"]: e for e in client.get(f"/api/tenants/{oc['a']['id']}/health").json()["events"]}
    assert ev["oci_maintenance"]["status"] == "upcoming" and "erp-app" in ev["oci_maintenance"]["title"]
    assert ev["oci_announcement"]["title"] == "Manutenção do Object Storage em São Paulo"
    assert client.get(f"/api/tenants/{oc['b']['id']}/health").json()["events"] == []


def test_usage_api_costs(client, oc, octx):
    _setup(client, octx, oc)
    with _db() as db:
        out = oci.collect_costs(db, octx, _conn(db, oc), today=date(2026, 10, 20))
        db.commit()
        assert _acc(db, fo.COMP_A).connection_id == oc["conn"]["id"]
    assert out == {"source": "oci_usage_api", "rows": {"2026-10": 1}}
    rep = client.get(
        f"/api/tenants/{oc['a']['id']}/reports/usage", params={"start": "2026-10-01", "end": "2026-10-31"}
    ).json()
    assert rep["totals"] == [{"currency": "BRL", "cost": "42.500000"}]


def test_focus_reports_from_oracle_bucket(client, oc, octx, fake):
    assert (
        client.patch(
            f"/api/admin/connections/{oc['conn']['id']}", json={"cost_source": "focus_oci"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/admin/connections/{oc['conn']['id']}", json={"cost_source": "cost_explorer"}
        ).status_code
        == 422
    )
    _setup(client, octx, oc)
    rows = [line(fo.TENANCY, fo.COMP_S, 5, "COMPUTE", 33, {"cost-center": "empresa-b"})]
    rows[0]["ChargePeriodStart"] = "2026-10-05T00:00:00Z"
    fake.objects["FOCUS Reports/2026/10/05/0001.csv.gz"] = gzip.compress(
        focus_csv(rows).replace(b"AWS", b"Oracle")
    )
    with _db() as db:
        out = oci.collect_costs(db, octx, _conn(db, oc), today=date(2026, 10, 20))
        db.commit()
    assert out["files"]["2026-10"] == 1
    rep = client.get(
        f"/api/tenants/{oc['b']['id']}/reports/usage", params={"start": "2026-10-01", "end": "2026-10-31"}
    ).json()
    assert Decimal(rep["totals"][0]["cost"]) == 33


def test_cloud_advisor(client, oc, octx, fake):
    _setup(client, octx, oc)
    _inventory(octx, oc)
    with _db() as db:
        assert oci.collect_optimization(db, octx, _conn(db, oc)) == {"cloud_advisor": 1}
        db.commit()
    rec = next(
        r
        for r in client.get(f"/api/tenants/{oc['a']['id']}/recommendations").json()["items"]
        if r["source"] == "oci_cloud_advisor"
    )
    assert rec["action_type"] == "Rightsize" and Decimal(rec["monthly_savings"]) == Decimal("120.40")
    assert rec["title"] == "compute-rightsizing: erp-app"


def test_instance_action_through_worker(client, oc, octx, fake):
    from datetime import UTC, datetime

    from app import worker as wmod

    _setup(client, octx, oc)
    _inventory(octx, oc)
    inst = client.get(f"/api/tenants/{oc['a']['id']}/resources", params={"type": "oci_instance"}).json()[0]
    r = client.post(
        f"/api/tenants/{oc['a']['id']}/resources/{inst['id']}/actions",
        json={"action": "stop", "confirm": "erp-app"},
    )
    assert r.status_code == 202, r.text
    w = wmod.Worker(only={"action"}, schedule=False)
    w._ctx = (datetime.now(UTC).timestamp() + 10_000, octx)
    assert w.run_once()
    act = client.get(f"/api/tenants/{oc['a']['id']}/actions").json()[0]
    assert act["status"] == "succeeded", act["result"]
    assert act["result"]["state"] == "stopping"
    assert ("POST", f"iaas/20160918/instances/{fo.INST_A}") in fake.calls


def test_scheduler_and_hub(client, oc, vault_keys):
    with _db() as db:
        kinds = {k.split(":")[0] for k in scheduler.tick(db)}
        db.commit()
    assert (
        {"discover", "costs", "optimization"} <= kinds
        and "invoices" not in kinds
        and "aws_health" not in kinds
    )

    pending = client.post(
        "/api/admin/connections/oci",
        json={
            "name": "Sem usuário",
            "tenancy_id": "ocid1.tenancy.oc1..aaaaaaaaoutrocliente000000000000000000",
            "home_region": fo.REGION,
        },
    )
    assert pending.status_code == 201
    with _db() as db:
        assert not db.get(Connection, pending.json()["id"]).collectable  # aguardando o OCID do usuário

    hub = client.put(
        "/api/admin/oci/hub",
        json={
            "tenancy_id": "ocid1.tenancy.oc1..aaaaaaaaplataforma000000000000000000000",
            "user_id": USER,
            "home_region": fo.REGION,
        },
    )
    assert (
        hub.status_code == 200
        and hub.json()["configured"]
        and hub.json()["public_key"].startswith("-----BEGIN PUBLIC KEY")
    )
    with _db() as db:
        h = OciHub.from_settings(db, vault_keys)
        assert h.credential.fingerprint == hub.json()["fingerprint"]

    rot = client.post(f"/api/admin/connections/{oc['conn']['id']}/oci-key")
    assert rot.status_code == 200 and rot.json()["oci_fingerprint"] != oc["conn"]["oci_fingerprint"]


def test_fingerprint_format():
    from cryptography.hazmat.primitives import serialization

    priv, pub, fp = generate_api_key()
    der = serialization.load_pem_public_key(pub.encode()).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert fp == fingerprint(der) and len(fp) == 47
