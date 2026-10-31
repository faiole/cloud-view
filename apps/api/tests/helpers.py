import csv
import io
import json

from tests.conftest import login

AB, AC, AD = "111111111111", "222222222222", "333333333333"


def focus_csv(rows: list[dict]) -> bytes:
    cols = [
        "ProviderName",
        "BillingAccountId",
        "BillingAccountName",
        "SubAccountId",
        "SubAccountName",
        "ChargePeriodStart",
        "ServiceName",
        "RegionId",
        "ChargeCategory",
        "BillingCurrency",
        "BilledCost",
        "EffectiveCost",
        "Tags",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(
            {
                "ProviderName": "AWS",
                "BillingCurrency": "USD",
                "RegionId": "us-east-1",
                "ChargeCategory": "Usage",
                "EffectiveCost": r.get("BilledCost"),
                "Tags": json.dumps(r.pop("tags", {})),
                **r,
            }
        )
    return buf.getvalue().encode()


def line(payer, account, day, service, cost, tags=None, name=None):
    return {
        "BillingAccountId": payer,
        "BillingAccountName": f"Org {payer[:2]}",
        "SubAccountId": account,
        "SubAccountName": name or f"conta-{account}",
        "ChargePeriodStart": f"2026-09-{day:02d}T00:00:00Z",
        "ServiceName": service,
        "BilledCost": str(cost),
        "tags": tags or {},
    }


def admin(client, oidc):
    login(client, oidc, sub="adm", email="adm@msp.com", roles=["platform-admin"])


def import_file(client, data: bytes, name="focus.csv"):
    r = client.post("/api/admin/imports/focus", files={"file": (name, data, "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()


def account_id(client, native_id):
    return next(a["id"] for a in client.get("/api/admin/accounts").json() if a["native_id"] == native_id)


def assign(client, native_id, tenant_id, **kw):
    return client.post(
        f"/api/admin/accounts/{account_id(client, native_id)}/assignments",
        json={"tenant_id": tenant_id, **kw},
    )
