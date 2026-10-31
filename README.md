# Cloud View

Painel multi-cloud (AWS, Azure e OCI) e multi-cliente: custos e pagamentos, cost optimization,
saúde de instâncias e serviços, rede, inventário, ações rápidas, alertas, atualizações e
recomendações. Login com senha e MFA obrigatório; credenciais guardadas com segurança.

- Arquitetura completa: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- O que a Fase 0 entrega e como subir: [docs/FASE-0.md](docs/FASE-0.md)

## Estrutura

```
apps/api        API/BFF em FastAPI: login OIDC, sessões, RBAC por cliente, auditoria
apps/web        Painel em Next.js
infra/keycloak  Realm do Keycloak (senha + TOTP obrigatório) e imagem de produção
infra/terraform Conta hub na AWS: VPC, ECS Fargate, RDS, Redis, SQS, KMS, Secrets Manager, ALB, WAF
infra/local     Inicialização do Postgres local
onboarding/aws  CloudFormation das roles CloudViewReadOnly / CloudViewOperator (conta ou Organization)
```

## Rodar localmente

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

Abra http://localhost:3000 e entre com `admin@cloudview.local` / `ChangeMe!12345`.
No primeiro acesso o Keycloak pede uma nova senha e o cadastro do autenticador (TOTP).
