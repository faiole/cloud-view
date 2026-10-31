"""Execução das ações rápidas: AWS (role CloudViewOperator, restrita por tag) e Azure (role Cloud View Operator)."""

import re

from sqlalchemy.orm import Session

from app.collectors.common import Ctx, account_session, client, error_text, now
from app.models import ActionRequest, Resource

ACTIONS = {"reboot": "reboot_instances", "start": "start_instances", "stop": "stop_instances"}


def execute_action(db: Session, ctx: Ctx, req: ActionRequest) -> dict:
    res = db.get(Resource, req.resource_id)
    if res is None:
        raise ValueError("recurso não existe mais")
    conn = res.account.connection
    if conn is None or not conn.collectable:
        raise ValueError("conta sem conexão configurada")
    who = "cv-" + re.sub(r"[^\w+=,.@-]", "-", req.requested_by)
    req.status = "running"
    db.flush()
    try:
        if res.provider == "azure":
            from app.collectors import azure

            state = azure.vm_action(db, ctx, res, req.action)
        else:
            sess = account_session(ctx, conn, res.account.native_id, operator=True, who=who[:64])
            ec2 = client(sess, "ec2", res.region)
            getattr(ec2, ACTIONS[req.action])(InstanceIds=[res.native_id])
            state = ec2.describe_instances(InstanceIds=[res.native_id])["Reservations"][0]["Instances"][0][
                "State"
            ]["Name"]
    except Exception as e:  # noqa: BLE001
        req.status = "failed"
        req.finished_at = now()
        req.result = {"error": error_text(e)}
        db.flush()
        return req.result
    res.state = state
    req.status = "succeeded"
    req.finished_at = now()
    req.result = {"state": state}
    db.flush()
    return req.result
