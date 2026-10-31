from dataclasses import dataclass
from datetime import UTC, datetime

import boto3

from app.aws.session import BOTO_CONFIG, AwsHub, role_arn_for
from app.models import CloudAccount, Connection


@dataclass
class Ctx:
    hub: AwsHub
    storage: object | None = None


def now() -> datetime:
    return datetime.now(UTC)


def account_session(
    ctx: Ctx, conn: Connection, account_native_id: str, *, operator: bool = False, who: str = "cloudview"
):
    """Sessão na conta: a própria role da conexão (management/avulsa) ou a role padrão nas contas-membro."""
    partition = (conn.role_arn or "arn:aws:").split(":")[1] or "aws"
    if operator:
        arn = role_arn_for(account_native_id, conn.operator_role_name, partition)
    elif account_native_id == conn.native_id and conn.role_arn:
        arn = conn.role_arn
    else:
        arn = role_arn_for(account_native_id, conn.member_role_name, partition)
    return ctx.hub.assume(arn, conn.external_id, session_name=who)


def client(sess: boto3.Session, service: str, region: str = "us-east-1"):
    return sess.client(service, region_name=region, config=BOTO_CONFIG)


def paginate(c, op: str, key: str, **kw):
    for page in c.get_paginator(op).paginate(**kw):
        yield from page.get(key, [])


def tags_of(items: list | None) -> dict:
    return {t["Key"]: t.get("Value", "") for t in items or [] if "Key" in t}


def regions_for(ctx_sess: boto3.Session, conn: Connection) -> list[str]:
    if conn.regions:
        return list(conn.regions)
    ec2 = client(ctx_sess, "ec2", "us-east-1")
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return sorted(r["RegionName"] for r in resp["Regions"])


def error_text(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error", {})
        return f"{err.get('Code', e.__class__.__name__)}: {err.get('Message', '')}".strip(": ")
    return str(e) or e.__class__.__name__


def mark_account_error(acc: CloudAccount, e: Exception | None) -> None:
    acc.last_error = error_text(e)[:2000] if e else None
