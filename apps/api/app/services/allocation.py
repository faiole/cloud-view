"""Atribui cada linha de custo a um cliente a partir das regras de `AccountAssignment`.

O cliente fica gravado na própria linha (`cost_line.tenant_id`), o que permite RLS simples e
consultas rápidas. Sempre que uma regra muda, a conta é realocada inteira.

Prioridade: regra com tag > regra da conta inteira. Linhas sem regra ficam sem cliente.
"""

from datetime import date

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models import AccountAssignment, CostLine, HealthEvent, Resource


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
    """Recalcula a empresa dona de custos, recursos e eventos de saúde da conta."""
    _reallocate_costs(db, cloud_account_id)
    reallocate_resources(db, cloud_account_id)


def active_rules(db: Session, cloud_account_id: str, today: date) -> list[AccountAssignment]:
    rules = db.scalars(
        select(AccountAssignment).where(AccountAssignment.cloud_account_id == cloud_account_id)
    ).all()
    return [r for r in rules if (r.valid_from or date.min) <= today <= (r.valid_to or date.max)]


def owner_for_tags(rules: list[AccountAssignment], tags: dict) -> str | None:
    """Mesma prioridade dos custos: regra com tag > conta inteira."""
    for r in rules:
        if r.tag_key is not None and (tags or {}).get(r.tag_key) == r.tag_value:
            return r.tenant_id
    whole = [r for r in rules if r.tag_key is None]
    return whole[0].tenant_id if whole else None


def whole_account_owner(db: Session, cloud_account_id: str, today: date | None = None) -> str | None:
    rules = active_rules(db, cloud_account_id, today or date.today())
    whole = [r for r in rules if r.tag_key is None]
    return whole[0].tenant_id if whole else None


def reallocate_resources(db: Session, cloud_account_id: str, today: date | None = None) -> None:
    rules = active_rules(db, cloud_account_id, today or date.today())
    owners: dict[str, str | None] = {}
    for res in db.scalars(select(Resource).where(Resource.cloud_account_id == cloud_account_id)):
        res.tenant_id = owner_for_tags(rules, res.tags)
        owners[res.id] = res.tenant_id
    whole = [r for r in rules if r.tag_key is None]
    account_owner = whole[0].tenant_id if whole else None
    for ev in db.scalars(select(HealthEvent).where(HealthEvent.cloud_account_id == cloud_account_id)):
        # Evento de um recurso segue o recurso; evento da conta só aparece se a conta é de uma empresa só
        ev.tenant_id = owners.get(ev.resource_id) if ev.resource_id else account_owner
    db.flush()


def _reallocate_costs(db: Session, cloud_account_id: str) -> None:
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
