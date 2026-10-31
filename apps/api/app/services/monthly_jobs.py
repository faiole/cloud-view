"""Job mensal: no dia 3, gera o relatório do mês anterior de cada empresa com custos, guarda em
*Invoices e notas* (tipo "Relatório mensal") e envia por e-mail aos canais de e-mail da empresa."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.common import error_text
from app.core.config import get_settings
from app.models import CostLine, Document, NotificationChannel, NotificationDelivery, Tenant
from app.services import notify
from app.services.documents import store_document
from app.services.monthly_report import MONTHS_PT, build_monthly_pdf, month_bounds

GENERATE_ON_DAY = 3  # dá tempo para as nuvens fecharem os valores do mês


def report_number(month: date) -> str:
    return f"RM-{month:%Y-%m}"


def generate_monthly_reports(db: Session, storage, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    if today.day < GENERATE_ON_DAY:
        return {"skipped": f"relatórios saem a partir do dia {GENERATE_ON_DAY}"}
    month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    start, end = month_bounds(month)
    generated, emailed, failed = [], 0, []
    for t in db.scalars(select(Tenant)):
        has_costs = db.scalar(
            select(CostLine.id)
            .where(CostLine.tenant_id == t.id, CostLine.charge_date >= start, CostLine.charge_date <= end)
            .limit(1)
        )
        if not has_costs or db.scalar(
            select(Document.id).where(
                Document.tenant_id == t.id, Document.kind == "report", Document.number == report_number(month)
            )
        ):
            continue
        pdf, data = build_monthly_pdf(db, t, month)
        filename = f"relatorio-{t.slug}-{month:%Y-%m}.pdf"
        store_document(
            db,
            storage,
            tenant_id=t.id,
            data=pdf,
            filename=filename,
            kind="report",
            billing_period=month,
            uploaded_by="Cloud View",
            number=report_number(month),
            issuer="Cloud View",
            amount=next(iter(data["totals"].values()), None),
            currency=next(iter(data["totals"]), None),
        )
        generated.append(t.name)
        payload = {
            "type": "monthly_report",
            "severity": "info",
            "tenant": t.name,
            "title": f"Relatório de custos de {MONTHS_PT[month.month - 1]} de {month.year}",
            "message": "O relatório mensal está em anexo e também em Invoices e notas, no painel.",
            "link": f"{get_settings().public_url.rstrip('/')}/clientes/{t.id}/documentos",
        }
        for ch in db.scalars(
            select(NotificationChannel).where(
                NotificationChannel.tenant_id == t.id,
                NotificationChannel.type == "email",
                NotificationChannel.enabled.is_(True),
            )
        ):
            if "cost" not in (ch.categories or []):
                continue
            try:
                notify.send_email(ch, payload, attachments=[(filename, pdf, "application/pdf")])
                db.add(NotificationDelivery(alert_id=None, channel_id=ch.id, status="sent"))
                emailed += 1
            except Exception as e:  # noqa: BLE001 - e-mail falhou, o relatório continua no painel
                db.add(
                    NotificationDelivery(
                        alert_id=None, channel_id=ch.id, status="failed", error=error_text(e)[:2000]
                    )
                )
                failed.append(ch.name)
    db.flush()
    return {"month": f"{month:%Y-%m}", "generated": generated, "emailed": emailed, "email_failed": failed}
