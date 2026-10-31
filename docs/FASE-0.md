# Fase 0 — Fundação

## Entregue

| Item | Onde |
|---|---|
| Login com usuário + senha + **TOTP obrigatório** para todos (fluxo `browser-mfa`), política de senha forte, bloqueio por força bruta | `infra/keycloak/realm-cloudview.json` |
| BFF com OIDC Authorization Code + PKCE; tokens ficam no servidor, browser recebe só cookie `HttpOnly` | `apps/api/app/routers/auth.py`, `app/security/` |
| Sessão no Redis: 15 min de inatividade, 8 h no máximo, rotação no login, proteção CSRF | `app/security/session.py`, `app/security/deps.py` |
| **Step-up MFA**: excluir cliente e alterar acessos exige senha + código de novo se o último MFA tiver mais de 5 min | `require_recent_mfa` |
| Clientes (tenants), papéis por cliente (`operator`, `viewer`, `client`) e administrador da plataforma | `app/routers/tenants.py` |
| Revogação imediata: os vínculos são relidos a cada request | `current_session` |
| **Row-Level Security** no Postgres + usuário da aplicação sem DDL | `migrations/versions/0001_foundation.py` |
| Auditoria **imutável** (trigger bloqueia UPDATE/DELETE) de logins e alterações | `app/services/audit.py` |
| ExternalId único por cliente para as roles AWS | modelo `Tenant` |
| Painel: visão geral, clientes, acessos, auditoria | `apps/web` |
| Stack Docker única (local e servidor) com Caddy e HTTPS automático | `docker-compose.yml` |
| Terraform opcional para AWS gerenciada | `infra/optional/aws-ecs` |
| Template de onboarding AWS (conta e Organization via StackSet) | `onboarding/aws` |
| CI: lint + testes (incluindo RLS em Postgres real), build do front, `terraform validate` | `.github/workflows/ci.yml` |

## Segurança das credenciais

Cofre portátil (`apps/api/app/vault.py`): a API grava credenciais com a chave pública, só os workers
têm a chave privada. Worker de coleta assume apenas `CloudViewReadOnly`; ações usam `CloudViewOperator`.

## Deploy

Veja [DEPLOY.md](DEPLOY.md): a stack roda com `docker compose` na máquina local ou numa VM de
qualquer nuvem. O Terraform de ECS virou opcional (`infra/optional/aws-ecs`).

## Testes

```bash
cd apps/api && uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest -q
```

Os testes de RLS rodam quando `CV_TEST_PG_OWNER_URL` e `CV_TEST_PG_APP_URL` apontam para um Postgres
inicializado com `infra/local/postgres-init.sh` (o CI faz isso).

## Ainda não validado

- `docker compose up` completo no ambiente de desenvolvimento (sem Docker); o CI sobe a stack.
- Login real contra o Keycloak: o fluxo foi testado com um IdP simulado.

## Próximo: Fase 1

Conexões AWS / Organizations, workers de coleta (inventário, rede, custos, saúde) e ações rápidas.
