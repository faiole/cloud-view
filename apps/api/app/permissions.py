"""Papéis por cliente e as permissões de cada um.

O administrador da plataforma (role `platform-admin` no Keycloak) tem todas as permissões em todos
os clientes. Os demais usuários recebem um papel por cliente; o mesmo usuário pode ter papéis
diferentes em clientes diferentes.
"""

import enum


class Perm(enum.StrEnum):
    resources_read = "resources.read"  # inventário, saúde, rede
    costs_read = "costs.read"  # custos e consumo
    reports_read = "reports.read"  # relatórios e exportação CSV
    invoices_read = "invoices.read"  # baixar invoices e notas fiscais
    actions_execute = "actions.execute"  # ações rápidas (reiniciar, ligar, desligar)
    members_manage = "members.manage"  # convidar/remover usuários do próprio cliente


class TenantRole(enum.StrEnum):
    tenant_admin = "tenant_admin"
    operator = "operator"
    finance = "finance"
    viewer = "viewer"


ROLE_PERMS: dict[TenantRole, frozenset[Perm]] = {
    TenantRole.tenant_admin: frozenset(Perm),
    TenantRole.operator: frozenset(
        {Perm.resources_read, Perm.costs_read, Perm.reports_read, Perm.actions_execute}
    ),
    TenantRole.finance: frozenset({Perm.costs_read, Perm.reports_read, Perm.invoices_read}),
    TenantRole.viewer: frozenset({Perm.resources_read, Perm.costs_read, Perm.reports_read}),
}

ROLE_LABELS = {
    TenantRole.tenant_admin: "Administrador da empresa",
    TenantRole.operator: "Operador",
    TenantRole.finance: "Financeiro",
    TenantRole.viewer: "Leitura",
}


def perms_for(role: str | None) -> frozenset[Perm]:
    try:
        return ROLE_PERMS[TenantRole(role)] if role else frozenset()
    except ValueError:
        return frozenset()
