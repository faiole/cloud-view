# Cloud View

Painel multi-cloud (AWS, Azure e OCI) e multi-empresa: custos e faturamento, relatórios por conta
e tag, invoices e notas fiscais, saúde, rede, ações rápidas e alertas. Login com senha e MFA
obrigatório; cada empresa vê só os próprios dados, mesmo com contas espalhadas em várias
Organizations.

## Começar

```bash
./scripts/setup.sh
```

```bash
docker compose up -d --build
```

```bash
docker compose run --rm keycloak-seed
```

Abra http://localhost:8080. Para servidor com HTTPS (qualquer nuvem), veja [docs/DEPLOY.md](docs/DEPLOY.md).

## Documentação
- [Arquitetura](docs/ARCHITECTURE.md)
- [Acessos, custos e faturamento por empresa](docs/ACESSOS-E-FATURAMENTO.md)
- [Como subir (local e servidor)](docs/DEPLOY.md)
- [Fase 0](docs/FASE-0.md)

## Estrutura

```
docker-compose.yml  stack completa: Caddy, painel, API, Keycloak, Postgres, Redis, backup
apps/api            FastAPI: login OIDC, papéis, custos (FOCUS), relatórios, documentos, cofre
apps/web            Painel em Next.js
infra/              Caddy, Keycloak, init do Postgres, Terraform opcional (aws-ecs)
onboarding/aws      CloudFormation das roles CloudViewReadOnly / CloudViewOperator
samples/            Arquivo FOCUS de exemplo (empresa A em 3 Organizations)
```
