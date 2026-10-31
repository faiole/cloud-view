"""Job `notify`: entrega um alerta (ou um teste) aos canais configurados."""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.common import error_text
from app.models import Alert, NotificationChannel, NotificationDelivery, Tenant
from app.services import notify
from app.services.alerts import channels_for
from app.services.secrets import read_secret


def _secret(db: Session, vault, ch: NotificationChannel) -> dict | None:
    if not ch.secret_ref:
        return None
    return json.loads(read_secret(db, vault, ch.secret_ref))


def process(db: Session, vault, payload: dict) -> dict:
    if payload.get("test"):
        ch = db.get(NotificationChannel, payload["channel_id"])
        if ch is None:
            return {"skipped": "canal removido"}
        try:
            notify.deliver(ch, notify.payload(None, None), _secret(db, vault, ch))
            db.add(NotificationDelivery(alert_id=None, channel_id=ch.id, status="sent"))
            return {"test": "sent"}
        except Exception as e:  # noqa: BLE001
            db.add(
                NotificationDelivery(
                    alert_id=None, channel_id=ch.id, status="failed", error=error_text(e)[:2000]
                )
            )
            return {"test": "failed", "error": error_text(e)}

    alert = db.get(Alert, payload["alert_id"])
    if alert is None or alert.status == "resolved":
        return {"skipped": "alerta resolvido ou removido"}
    tenant = db.get(Tenant, alert.tenant_id) if alert.tenant_id else None
    p = notify.payload(alert, tenant.name if tenant else None)
    sent, failed = [], []
    for ch in channels_for(db, alert):
        already = db.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.alert_id == alert.id,
                NotificationDelivery.channel_id == ch.id,
                NotificationDelivery.status == "sent",
            )
        )
        if already:
            continue
        old = db.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.alert_id == alert.id, NotificationDelivery.channel_id == ch.id
            )
        )
        try:
            notify.deliver(ch, p, _secret(db, vault, ch))
            status, err = "sent", None
            sent.append(ch.name)
        except Exception as e:  # noqa: BLE001
            status, err = "failed", error_text(e)[:2000]
            failed.append(f"{ch.name}: {err}")
        if old:
            old.status, old.error = status, err
        else:
            db.add(NotificationDelivery(alert_id=alert.id, channel_id=ch.id, status=status, error=err))
    db.flush()
    if failed and not sent:
        # Todos falharam: o job é repetido (com espera crescente) para tentar de novo
        raise RuntimeError("; ".join(failed))
    return {"sent": sent, "failed": failed}
