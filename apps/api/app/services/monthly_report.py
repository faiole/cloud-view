"""Relatório mensal da empresa em PDF: custos, comparação, orçamentos, otimização, alertas e infraestrutura.

`report_data` monta os números (testável); `render_pdf` desenha com reportlab (Python puro).
"""

import io
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Alert, Budget, Recommendation, Resource, Tenant
from app.resource_types import INSTANCE_TYPES
from app.services import budgets as budget_svc
from app.services import reports as rep

MONTHS_PT = [
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]
TAG_PREFERENCE = (
    "cost-center",
    "centro-de-custo",
    "projeto",
    "project",
    "ambiente",
    "environment",
    "env",
    "owner",
)
INK = colors.HexColor("#17202c")
MUTED = colors.HexColor("#5d6b7c")
LINE = colors.HexColor("#dfe5ec")
ACCENT = colors.HexColor("#1f6feb")
BAD = colors.HexColor("#c2261d")


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    return first, first.replace(day=monthrange(first.year, first.month)[1])


def _totals(db: Session, tid: str, start: date, end: date) -> dict[str, Decimal]:
    return {
        t["currency"]: Decimal(t["cost"]) for t in rep.totals(db, rep.base_filters(tid, start, end), "billed")
    }


def report_data(db: Session, tenant: Tenant, month: date) -> dict:
    start, end = month_bounds(month)
    prev_start, prev_end = month_bounds(start - timedelta(days=1))
    conds = rep.base_filters(tenant.id, start, end)
    cur, prev = _totals(db, tenant.id, start, end), _totals(db, tenant.id, prev_start, prev_end)

    trend = []
    m = start
    for _ in range(6):
        s, e = month_bounds(m)
        trend.append({"month": s, "totals": _totals(db, tenant.id, s, e)})
        m = s - timedelta(days=1)
    trend.reverse()

    tag_key = None
    keys = rep.tag_keys(db, tenant.id)
    for k in TAG_PREFERENCE:
        if k in keys:
            tag_key = k
            break
    tag_key = tag_key or (keys[0] if keys else None)

    budgets = []
    for b in db.scalars(select(Budget).where(Budget.tenant_id == tenant.id).order_by(Budget.name)):
        actual, _, _ = budget_svc.compute(db, b, end)
        budgets.append(
            {
                "name": b.name,
                "amount": Decimal(b.amount),
                "actual": actual,
                "currency": b.currency,
                "pct": (actual / Decimal(b.amount) * 100) if b.amount else Decimal(0),
            }
        )

    recs = db.scalars(
        select(Recommendation)
        .where(Recommendation.tenant_id == tenant.id, Recommendation.status == "open")
        .order_by(Recommendation.monthly_savings.desc())
    ).all()
    savings: dict[str, Decimal] = {}
    for r in recs:
        savings[r.currency] = savings.get(r.currency, Decimal(0)) + Decimal(r.monthly_savings or 0)

    month_start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    month_end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    alerts = dict(
        db.execute(
            select(Alert.category, func.count())
            .where(
                Alert.tenant_id == tenant.id,
                Alert.created_at >= month_start_dt,
                Alert.created_at < month_end_dt,
            )
            .group_by(Alert.category)
        ).all()
    )
    instances = db.scalars(
        select(Resource).where(Resource.tenant_id == tenant.id, Resource.type.in_(INSTANCE_TYPES))
    ).all()

    return {
        "tenant": tenant.name,
        "month": start,
        "generated_at": datetime.now(UTC),
        "totals": cur,
        "previous": prev,
        "trend": trend,
        "by_account": rep.usage(db, conds, "account", "billed", limit=15),
        "by_service": rep.usage(db, conds, "service", "billed", limit=12),
        "by_region": rep.usage(db, conds, "region", "billed", limit=8),
        "tag_key": tag_key,
        "by_tag": rep.usage(db, conds, f"tag:{tag_key}", "billed", limit=10) if tag_key else [],
        "budgets": budgets,
        "recommendations": recs[:10],
        "recommendations_open": len(recs),
        "savings": savings,
        "alerts": alerts,
        "instances": len(instances),
        "running": sum(1 for i in instances if i.state == "running"),
        "exposed": sum(
            1
            for i in instances
            if i.public_ips and any(f["code"].startswith("sg_") for f in i.findings or [])
        ),
    }


def money(v: Decimal | float, cur: str) -> str:
    s = f"{Decimal(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{cur} {s}"


def _totals_text(t: dict[str, Decimal]) -> str:
    return " + ".join(money(v, c) for c, v in t.items()) or "sem custos"


def _variation(cur: dict, prev: dict) -> str:
    parts = []
    for c, v in cur.items():
        p = prev.get(c)
        if p:
            pct = (v - p) / p * 100
            parts.append(
                f"{'+' if pct >= 0 else ''}{pct:.1f}".replace(".", ",")
                + f"% vs. mês anterior ({money(p, c)})"
            )
    return "; ".join(parts) or "sem base de comparação no mês anterior"


def render_pdf(data: dict) -> bytes:
    buf = io.BytesIO()
    title = f"Relatório de custos — {MONTHS_PT[data['month'].month - 1]} de {data['month'].year}"
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="Cloud View",
        subject=data["tenant"],
    )
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1",
        parent=ss["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        textColor=INK,
        alignment=0,
        spaceAfter=2,
    )
    h2 = ParagraphStyle(
        "h2",
        parent=ss["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        textColor=INK,
        spaceBefore=12,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, textColor=INK, leading=13
    )
    muted = ParagraphStyle("m", parent=body, textColor=MUTED, fontSize=8.5)
    big = ParagraphStyle("big", parent=body, fontName="Helvetica-Bold", fontSize=20, leading=24)

    def table(rows, widths, money_cols=(-1,)):
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]
        for c in money_cols:
            style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
        t.setStyle(TableStyle(style))
        return t

    story = [
        Paragraph(data["tenant"], muted),
        Paragraph(title, h1),
        Spacer(1, 6),
        Paragraph(_totals_text(data["totals"]), big),
        Paragraph(_variation(data["totals"], data["previous"]), muted),
        Spacer(1, 8),
    ]

    # Tendência de 6 meses (moeda principal)
    cur = next(iter(data["totals"]), None) or next((c for m in data["trend"] for c in m["totals"]), None)
    if cur:
        values = [float(m["totals"].get(cur, 0)) for m in data["trend"]]
        d = Drawing(170 * mm, 52 * mm)
        ch = VerticalBarChart()
        ch.x, ch.y, ch.width, ch.height = 12 * mm, 8 * mm, 150 * mm, 38 * mm
        ch.data = [values]
        ch.categoryAxis.categoryNames = [
            f"{MONTHS_PT[m['month'].month - 1][:3]}/{m['month']:%y}" for m in data["trend"]
        ]
        ch.categoryAxis.labels.fontSize = 7
        ch.valueAxis.labels.fontSize = 7
        ch.valueAxis.valueMin = 0
        ch.valueAxis.strokeColor = LINE
        ch.categoryAxis.strokeColor = LINE
        ch.bars[0].fillColor = ACCENT
        ch.bars[0].strokeColor = None
        ch.bars[(0, len(values) - 1)].fillColor = INK
        d.add(ch)
        d.add(
            String(0, 48 * mm, f"Últimos 6 meses ({cur})", fontName="Helvetica", fontSize=8, fillColor=MUTED)
        )
        story.append(d)

    def usage_table(rows, label):
        if not rows:
            return [Paragraph("Sem dados.", muted)]
        total = sum(Decimal(r["cost"]) for r in rows) or Decimal(1)
        out = [[label, "Custo", "%"]]
        for r in rows:
            out.append(
                [
                    Paragraph(r["label"], body),
                    money(r["cost"], r["currency"]),
                    f"{Decimal(r['cost']) / total * 100:.1f}%".replace(".", ","),
                ]
            )
        return [table(out, [110 * mm, 40 * mm, 20 * mm], money_cols=(1, 2))]

    # Título e tabela sempre na mesma página
    story.append(KeepTogether([Paragraph("Por conta", h2), *usage_table(data["by_account"], "Conta")]))
    story.append(
        KeepTogether([Paragraph("Principais serviços", h2), *usage_table(data["by_service"], "Serviço")])
    )
    if data["tag_key"]:
        story.append(
            KeepTogether(
                [Paragraph(f"Por tag “{data['tag_key']}”", h2), *usage_table(data["by_tag"], "Valor")]
            )
        )
    story.append(KeepTogether([Paragraph("Por região", h2), *usage_table(data["by_region"], "Região")]))

    if data["budgets"]:
        rows = [["Orçamento", "Realizado", "Valor", "Uso"]]
        for b in data["budgets"]:
            rows.append(
                [
                    b["name"],
                    money(b["actual"], b["currency"]),
                    money(b["amount"], b["currency"]),
                    f"{b['pct']:.0f}%",
                ]
            )
        t = table(rows, [70 * mm, 40 * mm, 40 * mm, 20 * mm], money_cols=(1, 2, 3))
        for i, b in enumerate(data["budgets"], start=1):
            if b["pct"] >= 100:
                t.setStyle(TableStyle([("TEXTCOLOR", (3, i), (3, i), BAD)]))
        story.append(KeepTogether([Paragraph("Orçamentos", h2), t]))

    opt = [Paragraph("Otimização", h2)]
    if data["recommendations_open"]:
        opt.append(
            Paragraph(
                f"{data['recommendations_open']} recomendações abertas, economia possível de "
                f"<b>{_totals_text(data['savings'])}</b> por mês.",
                body,
            )
        )
        rows = [["Recomendação", "Economia/mês"]]
        for r in data["recommendations"]:
            rows.append(
                [
                    Paragraph(r.title, body),
                    money(r.monthly_savings, r.currency) + ("*" if r.estimated else ""),
                ]
            )
        opt += [
            Spacer(1, 4),
            table(rows, [140 * mm, 30 * mm], money_cols=(1,)),
            Paragraph("* estimativa por preço de tabela.", muted),
        ]
    else:
        opt.append(Paragraph("Nenhuma recomendação aberta.", body))
    story.append(KeepTogether(opt))

    a = data["alerts"]
    story += [
        Paragraph("Operação no mês", h2),
        table(
            [
                ["Indicador", "Valor"],
                ["Alertas de custo", str(a.get("cost", 0))],
                ["Alertas de saúde", str(a.get("health", 0))],
                ["Alertas de segurança", str(a.get("security", 0))],
                ["Instâncias (ligadas / total)", f"{data['running']} / {data['instances']}"],
                ["Instâncias expostas à internet hoje", str(data["exposed"])],
            ],
            [130 * mm, 40 * mm],
            money_cols=(1,),
        ),
        Spacer(1, 14),
        Paragraph(
            f"Gerado pelo Cloud View em {data['generated_at']:%d/%m/%Y %H:%M} UTC. Valores faturados no mês, "
            "como reportados pelas nuvens; o mês corrente pode mudar até o fechamento.",
            muted,
        ),
    ]

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"Cloud View · {data['tenant']}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"página {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def build_monthly_pdf(db: Session, tenant: Tenant, month: date) -> tuple[bytes, dict]:
    data = report_data(db, tenant, month)
    return render_pdf(data), data
