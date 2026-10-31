"""Atribui cada linha de custo a um cliente a partir das regras de `AccountAssignment`.

O cliente fica gravado na própria linha (`cost_line.tenant_id`), o que permite RLS simples e
consultas rápidas. Sempre que uma regra muda, a conta é realocada inteira.

Prioridade: regra com tag > regra da conta inteira. Linhas sem regra ficam sem cliente.
"""

from datetime import date

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models import AccountAssignment, CostLine


class AllocationConflict(ValueError):
    pass


def _overlaps(a_from: date | None, a_to: date | None, b_from: date | None, b_to: date | None) -> bool:
    return (a_from or date.min) <= (b_to or date.max) and (b_from or date.min) <= (a_to or date.max)


def validate_rule(db: Session, new: AccountAssignment) -> None:
    """Impede regras ambíguas, em que a mesma linha de custo poderia ir para dois clientes."""
    if (new.tag_key is None) != (new.tag_value is None):
        raise AllocationConflict("tag_key e tag_value devem vir juntos")
    if new.valid_from and new.valid_to and new.valid_to < new.valid_from:
        raise AllocationConflict("valid_to antes de valid_from")
    others = db.scalars(
        select(AccountAssignment).where(
            AccountAssignment.cloud_account_id == new.cloud_account_id, AccountAssignment.id != new.id
        )
    ).all()
    for o in others:
        if not _overlaps(new.valid_from, new.valid_to, o.valid_from, o.valid_to):
            continue
        if new.tag_key is None and o.tag_key is None:
            raise AllocationConflict("a conta já está atribuída inteira a um cliente nesse período")
        if new.tag_key and o.tag_key and new.tag_key != o.tag_key:
            raise AllocationConflict(f"esta conta já é dividida pela tag '{o.tag_key}'; use a mesma chave")
        if new.tag_key and new.tag_key == o.tag_key and new.tag_value == o.tag_value:
            raise AllocationConflict("já existe regra para essa tag nesse período")


def _date_filter(rule: AccountAssignment):
    conds = []
    if rule.valid_from:
        conds.append(CostLine.charge_date >= rule.valid_from)
    if rule.valid_to:
        conds.append(CostLine.charge_date <= rule.valid_to)
    return conds


def reallocate_account(db: Session, cloud_account_id: str) -> None:
    rules = db.scalars(
        select(AccountAssignment).where(AccountAssignment.cloud_account_id == cloud_account_id)
    ).all()
    base = CostLine.cloud_account_id == cloud_account_id

    db.execute(
        update(CostLine).where(base).values(tenant_id=None).execution_options(synchronize_session=False)
    )
    for r in sorted(rules, key=lambda r: r.tag_key is not None):  # conta inteira primeiro, tags depois
        conds = [base, *_date_filter(r)]
        if r.tag_key is not None:
            conds.append(CostLine.tags[r.tag_key].as_string() == r.tag_value)
        db.execute(
            update(CostLine)
            .where(and_(*conds))
            .values(tenant_id=r.tenant_id)
            .execution_options(synchronize_session=False)
        )
    db.flush()
