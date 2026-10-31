"""Empresa A tem contas espalhadas em três Organizations (AB, AC, AD), que também têm contas de
outras empresas. A só pode ver os próprios custos."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login
from tests.helpers import AB, AC, AD, admin, assign, focus_csv, import_file, line

PERIOD = {"start": "2026-09-01", "end": "2026-09-30"}


@pytest.fixture
def world(client, oidc):
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "Empresa A", "slug": "empresa-a"}).json()
    b = client.post("/api/tenants", json={"name": "Empresa B", "slug": "empresa-b"}).json()

    import_file(
        client,
        focus_csv(
            [
                line(AB, "A00000000001", 1, "Amazon EC2", 100, {"user:projeto": "erp"}),
                line(AB, "A00000000001", 2, "Amazon S3", 10, {"user:projeto": "site"}),
                line(AB, "B00000000001", 1, "Amazon EC2", 999),  # empresa B, mesma Organization
            ]
        ),
        "ab.csv",
    )
    import_file(
        client,
        focus_csv(
            [
                line(AC, "A00000000002", 3, "Amazon RDS", 50, {"user:projeto": "erp"}),
                # conta compartilhada, dividida pela tag cost-center
                line(AC, "S00000000001", 3, "Amazon EC2", 30, {"user:cost-center": "empresa-a"}),
                line(AC, "S00000000001", 3, "Amazon EC2", 70, {"user:cost-center": "empresa-b"}),
                line(AC, "S00000000001", 3, "AWS Support", 5),  # sem tag: fica sem dono
            ]
        ),
        "ac.csv",
    )
    import_file(client, focus_csv([line(AD, "A00000000003", 4, "AWS Lambda", 7)]), "ad.csv")

    for acc in ("A00000000001", "A00000000002", "A00000000003"):
        assert assign(client, acc, a["id"]).status_code == 201
    assert assign(client, "B00000000001", b["id"]).status_code == 201
    assert (
        assign(client, "S00000000001", a["id"], tag_key="cost-center", tag_value="empresa-a").status_code
        == 201
    )
    assert (
        assign(client, "S00000000001", b["id"], tag_key="cost-center", tag_value="empresa-b").status_code
        == 201
    )

    for email, tid in (("fin@a.com", a["id"]), ("fin@b.com", b["id"])):
        client.put(f"/api/tenants/{tid}/members", json={"email": email, "role": "finance"})
    return a, b


def _as(oidc, sub, email):
    c = TestClient(app, base_url="http://testserver")
    login(c, oidc, sub=sub, email=email)
    return c


def _usage(c, tid, group_by, **params):
    r = c.get(f"/api/tenants/{tid}/reports/usage", params={**PERIOD, "group_by": group_by, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_company_sees_only_its_accounts_across_organizations(client, oidc, world):
    a, b = world
    ca = _as(oidc, "fa", "fin@a.com")
    rep = _usage(ca, a["id"], "account")
    assert Decimal(rep["totals"][0]["cost"]) == Decimal("197")  # 100+10+50+30+7
    assert {r["key"] for r in rep["rows"]} == {"A00000000001", "A00000000002", "A00000000003", "S00000000001"}

    cb = _as(oidc, "fb", "fin@b.com")
    assert Decimal(_usage(cb, b["id"], "account")["totals"][0]["cost"]) == Decimal("1069")  # 999+70
    # A não alcança o cliente B (nem descobre que existe)
    assert ca.get(f"/api/tenants/{b['id']}/reports/usage", params=PERIOD).status_code == 404


def test_tag_report_and_filter(client, oidc, world):
    a, _ = world
    ca = _as(oidc, "fa", "fin@a.com")
    rows = {r["key"]: Decimal(r["cost"]) for r in _usage(ca, a["id"], "tag:projeto")["rows"]}
    assert rows == {"erp": Decimal(150), "site": Decimal(10), "(sem tag)": Decimal(37)}
    only_erp = _usage(ca, a["id"], "service", tag="projeto=erp")
    assert {r["key"] for r in only_erp["rows"]} == {"Amazon EC2", "Amazon RDS"}
    assert ca.get(f"/api/tenants/{a['id']}/tags").json()["keys"] == ["cost-center", "projeto"]


def test_csv_export_contains_only_own_lines(client, oidc, world):
    a, _ = world
    ca = _as(oidc, "fa", "fin@a.com")
    r = ca.get(f"/api/tenants/{a['id']}/reports/usage.csv", params=PERIOD)
    assert r.status_code == 200
    body = r.text
    assert "B00000000001" not in body and "999" not in body
    assert "conta_pagadora" not in body  # id da pagadora compartilhada não aparece para o cliente
    assert body.count("\n") == 6  # cabeçalho + 5 linhas

    st = ca.get(f"/api/tenants/{a['id']}/reports/statement.csv", params={"month": "2026-09"})
    assert st.status_code == 200 and "A00000000003" in st.text and "B00000000001" not in st.text


def test_unassigned_costs_stay_with_admin(client, oidc, world):
    rows = client.get("/api/admin/accounts", params={"unassigned": "true"}).json()
    assert rows == []
    # A linha sem tag da conta compartilhada não aparece para nenhum cliente
    a, b = world
    for tid in (a["id"], b["id"]):
        assert "AWS Support" not in {r["key"] for r in _usage(client, tid, "service")["rows"]}


def test_reimport_replaces_instead_of_duplicating(client, oidc, world):
    a, _ = world
    data = focus_csv([line(AD, "A00000000003", 4, "AWS Lambda", 7)])
    r = import_file(client, data, "ad.csv")
    assert r["replaced_rows"] == 1
    assert Decimal(_usage(client, a["id"], "account")["totals"][0]["cost"]) == Decimal("197")


def test_account_moved_to_another_organization_keeps_owner(client, oidc, world):
    a, _ = world
    import_file(
        client,
        focus_csv([{**line(AD, "A00000000002", 20, "Amazon RDS", 12)}]),
        "ad-oct.csv",
    )
    acc = next(x for x in client.get("/api/admin/accounts").json() if x["native_id"] == "A00000000002")
    assert acc["connection_native_id"] == AD
    assert Decimal(_usage(client, a["id"], "account")["totals"][0]["cost"]) == Decimal("209")


def test_new_accounts_are_reported_as_unassigned(client, oidc, world):
    r = import_file(client, focus_csv([line(AB, "N00000000001", 5, "Amazon EC2", 1)]), "new.csv")
    assert r["new_accounts"] == ["N00000000001"]
    assert r["unassigned_accounts"] == ["N00000000001"]


def test_assignment_validity_window(client, oidc):
    admin(client, oidc)
    t = client.post("/api/tenants", json={"name": "T", "slug": "t"}).json()
    import_file(client, focus_csv([line(AB, "X00000000001", d, "EC2", 1) for d in (10, 14, 15, 20)]))
    assert assign(client, "X00000000001", t["id"], valid_from="2026-09-15").status_code == 201
    assert Decimal(_usage(client, t["id"], "account")["totals"][0]["cost"]) == Decimal(2)


def test_conflicting_rules_are_rejected(client, oidc):
    admin(client, oidc)
    t1 = client.post("/api/tenants", json={"name": "T1", "slug": "t1"}).json()
    t2 = client.post("/api/tenants", json={"name": "T2", "slug": "t2"}).json()
    import_file(client, focus_csv([line(AB, "X00000000001", 1, "EC2", 1)]))
    assert assign(client, "X00000000001", t1["id"]).status_code == 201
    assert assign(client, "X00000000001", t2["id"]).status_code == 409
    assert assign(client, "X00000000001", t2["id"], tag_key="cc", tag_value="t2").status_code == 201
    assert assign(client, "X00000000001", t2["id"], tag_key="outra", tag_value="x").status_code == 409
    # Contas que trocaram de dono: janelas sem sobreposição são aceitas
    r = client.get("/api/admin/accounts").json()[0]["id"]
    rules = client.get(f"/api/admin/accounts/{r}/assignments").json()
    whole = next(x for x in rules if x["tag_key"] is None)
    assert client.delete(f"/api/admin/assignments/{whole['id']}").status_code == 204
    assert assign(client, "X00000000001", t1["id"], valid_to="2026-08-31").status_code == 201
    assert assign(client, "X00000000001", t2["id"], valid_from="2026-09-01").status_code == 201


def test_invalid_focus_file(client, oidc):
    admin(client, oidc)
    r = client.post("/api/admin/imports/focus", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422 and "BilledCost" in r.json()["detail"]


def test_only_admin_imports_and_assigns(client, oidc, world):
    ca = _as(oidc, "fa", "fin@a.com")
    data = focus_csv([line(AB, "A00000000001", 1, "EC2", 1)])
    assert ca.post("/api/admin/imports/focus", files={"file": ("f.csv", data, "text/csv")}).status_code == 403
    assert ca.get("/api/admin/accounts").status_code == 403


def test_deleting_company_releases_its_costs(client, oidc, world):
    a, _ = world
    assert client.delete(f"/api/tenants/{a['id']}").status_code == 204
    unassigned = {
        x["native_id"] for x in client.get("/api/admin/accounts", params={"unassigned": "true"}).json()
    }
    assert {"A00000000001", "A00000000002", "A00000000003"} <= unassigned
