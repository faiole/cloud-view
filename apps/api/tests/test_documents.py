import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login
from tests.helpers import admin

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
NFE = b'<?xml version="1.0" encoding="UTF-8"?><nfeProc versao="4.00"></nfeProc>'


@pytest.fixture
def tenants(client, oidc):
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "A", "slug": "a"}).json()
    b = client.post("/api/tenants", json={"name": "B", "slug": "b"}).json()
    for email, tid, role in (
        ("fin@a.com", a["id"], "finance"),
        ("ops@a.com", a["id"], "operator"),
        ("fin@b.com", b["id"], "finance"),
    ):
        client.put(f"/api/tenants/{tid}/members", json={"email": email, "role": role})
    return a, b


def _upload(client, tid, data=PDF, name="fatura-set.pdf", **form):
    return client.post(
        f"/api/tenants/{tid}/documents",
        files={"file": (name, data, "application/octet-stream")},
        data={"kind": "invoice", "billing_period": "2026-09", **form},
    )


def _as(oidc, sub, email):
    c = TestClient(app, base_url="http://testserver")
    login(c, oidc, sub=sub, email=email)
    return c


def test_finance_downloads_own_documents_only(client, oidc, tenants):
    a, b = tenants
    doc = _upload(client, a["id"], number="INV-123", amount="1500.50", currency="usd").json()
    nf = _upload(client, a["id"], data=NFE, name="nfe 001.xml", kind="nota_fiscal").json()
    assert doc["content_type"] == "application/pdf" and doc["currency"] == "USD"
    assert nf["content_type"] == "application/xml" and nf["filename"] == "nfe_001.xml"

    fa = _as(oidc, "fa", "fin@a.com")
    assert {d["id"] for d in fa.get(f"/api/tenants/{a['id']}/documents").json()} == {doc["id"], nf["id"]}
    r = fa.get(f"/api/tenants/{a['id']}/documents/{doc['id']}/download")
    assert r.status_code == 200 and r.content == PDF
    assert "attachment" in r.headers["content-disposition"]

    fb = _as(oidc, "fb", "fin@b.com")
    # B não acessa o documento de A, nem trocando o cliente na URL
    assert fb.get(f"/api/tenants/{a['id']}/documents/{doc['id']}/download").status_code == 404
    assert fb.get(f"/api/tenants/{b['id']}/documents/{doc['id']}/download").status_code == 404
    assert fb.get(f"/api/tenants/{b['id']}/documents").json() == []

    downloads = [e for e in client.get("/api/audit", params={"action": "document.download"}).json()]
    assert downloads and downloads[0]["user_email"] == "fin@a.com"


def test_operator_cannot_see_invoices(client, oidc, tenants):
    a, _ = tenants
    doc = _upload(client, a["id"]).json()
    ops = _as(oidc, "op", "ops@a.com")
    # Quem vê custos enxerga só o relatório mensal; as faturas continuam restritas ao financeiro
    assert ops.get(f"/api/tenants/{a['id']}/documents").json() == []
    assert ops.get(f"/api/tenants/{a['id']}/documents/{doc['id']}/download").status_code == 403


def test_rejects_unknown_types_and_duplicates(client, oidc, tenants):
    a, _ = tenants
    assert _upload(client, a["id"], data=b"MZ\x90\x00 executavel", name="fatura.pdf").status_code == 415
    assert _upload(client, a["id"]).status_code == 201
    assert _upload(client, a["id"]).status_code == 409
    assert _upload(client, a["id"], data=PDF + b" ", billing_period="2026-13").status_code == 422


def test_only_admin_uploads_and_delete_needs_recent_mfa(client, oidc, tenants):
    a, _ = tenants
    fa = _as(oidc, "fa", "fin@a.com")
    assert _upload(fa, a["id"]).status_code == 403

    doc = _upload(client, a["id"]).json()
    from tests.test_tenants import _expire_mfa

    _expire_mfa(client)
    assert client.delete(f"/api/tenants/{a['id']}/documents/{doc['id']}").status_code == 401
    login(client, oidc, sub="adm", email="adm@msp.com", roles=["platform-admin"], step_up=True)
    assert client.delete(f"/api/tenants/{a['id']}/documents/{doc['id']}").status_code == 204
    assert client.get(f"/api/tenants/{a['id']}/documents").json() == []
