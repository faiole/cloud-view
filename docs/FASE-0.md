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
| Infra AWS completa da conta hub (Terraform validado) | `infra/terraform` |
| Template de onboarding AWS (conta e Organization via StackSet) | `onboarding/aws` |
| CI: lint + testes (incluindo RLS em Postgres real), build do front, `terraform validate` | `.github/workflows/ci.yml` |

## Segurança das credenciais (preparado para a Fase 1)

- Chave KMS **exclusiva** para credenciais de clientes. Pela key policy, só os workers decifram;
  a API só cifra (grava). A API também tem `Deny` explícito em `GetSecretValue` nesse prefixo.
- Regra EventBridge alerta se alguém fora dos workers ler uma credencial de cliente.
- Worker de coleta só pode assumir `CloudViewReadOnly`; só o worker de ações assume `CloudViewOperator`.

## Deploy na AWS

1. Crie um certificado ACM para `painel.<domínio>` e `auth.painel.<domínio>`.
2. Copie e preencha `infra/terraform/envs/prod.tfvars.example` e `prod.backend.hcl.example`.
3. Crie primeiro só os repositórios ECR:
   ```bash
   cd infra/terraform && terraform init -backend-config=envs/prod.backend.hcl
   ```
   ```bash
   terraform apply -var-file=envs/prod.tfvars -target=aws_ecr_repository.this
   ```
4. Publique as imagens `api`, `web` (`apps/*/Dockerfile`) e `keycloak` (`infra/keycloak/Dockerfile`)
   com a tag definida em `image_tag` (arquitetura **arm64**).
5. `terraform apply -var-file=envs/prod.tfvars`
6. Rode a task de migração (`migrate_task_definition`) nas subnets privadas. Ela cria os usuários
   do banco, o banco do Keycloak e aplica as migrações.
7. Crie o primeiro administrador com `kcadm.sh` (veja `infra/keycloak/dev-seed.sh`) via ECS Exec
   temporário ou túnel. O console admin do Keycloak fica bloqueado no ALB.

## Testes

```bash
cd apps/api && uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest -q
```

Os testes de RLS rodam quando `CV_TEST_PG_OWNER_URL` e `CV_TEST_PG_APP_URL` apontam para um Postgres
inicializado com `infra/local/postgres-init.sql` (o CI faz isso).

## Ainda não validado

- `docker compose up` completo (o ambiente de desenvolvimento não tinha Docker rodando).
- `terraform plan/apply` numa conta AWS real (apenas `validate`).
- Login real contra o Keycloak: o fluxo foi testado com um IdP simulado.

## Próximo: Fase 1

Conexões AWS / Organizations, workers de coleta (inventário, rede, custos, saúde) e ações rápidas.
