# Subir o Cloud View

A mesma stack Docker roda na sua máquina e em uma VM de qualquer nuvem. Requisitos: Docker 24+
com o plugin `compose`, e `openssl`.

## Na sua máquina

```bash
./scripts/setup.sh
```

```bash
docker compose up -d --build
```

```bash
docker compose run --rm keycloak-seed
```

Abra http://localhost:8080 e entre com o e-mail e a senha que o `setup.sh` mostrou. No primeiro
acesso o Keycloak pede uma senha nova e o cadastro do autenticador (TOTP).

- E-mails de convite chegam no Mailpit: http://localhost:8025
- Para ver dados, vá em *Contas de nuvem* e importe `samples/focus-exemplo-aws.csv`.

## Em um servidor (AWS, Azure, OCI ou on-premises)

1. Crie uma VM Linux (2 vCPU / 8 GB bastam para começar; na OCI a Ampere A1 cabe no *Always Free*).
2. Libere só as portas **80 e 443** no firewall / security group / NSG.
3. Aponte o DNS do domínio (ex.: `painel.suaempresa.com.br`) para o IP da VM.
4. Na VM:

```bash
git clone https://github.com/faiole/cloud-view.git && cd cloud-view
```

```bash
./scripts/setup.sh painel.suaempresa.com.br
```

5. Edite o `.env` e configure o SMTP real (convites e redefinição de senha) e, se quiser, a rede
   que pode abrir o console do Keycloak (`CV_ADMIN_CIDRS`).
6. Suba:

```bash
docker compose up -d --build
```

```bash
docker compose run --rm keycloak-seed
```

O Caddy emite o certificado HTTPS sozinho na primeira requisição.

## Operação

| Tarefa | Como |
|---|---|
| Atualizar | `git pull && docker compose up -d --build` (as migrações rodam sozinhas) |
| Logs | `docker compose logs -f api` |
| Backup | diário, automático, no volume `backups` (14 dias). Copie para fora da VM, ex.: `docker compose cp backup:/backups ./backups` |
| Restaurar | `docker compose exec -T postgres pg_restore -U postgres -d cloudview --clean < arquivo.dump` |
| Guardar fora da VM | o `.env` e o volume `vault-private` (sem eles não dá para ler as credenciais cifradas) |

## Crescendo
- **Banco gerenciado** (RDS, Azure Database, OCI PostgreSQL): troque a URL do banco no compose e
  rode a migração com `DB_MANAGE_ROLES=true`.
- **Documentos em S3 compatível**: `CV_STORAGE_BACKEND=s3`, `CV_DOCUMENTS_BUCKET` e
  `CV_S3_ENDPOINT_URL` (MinIO, OCI Object Storage, R2...). Na AWS deixe o endpoint vazio.
- **AWS com serviços gerenciados**: `infra/optional/aws-ecs` (opcional).

## Segurança
- Só o Caddy publica portas; banco, Redis, Keycloak e API ficam na rede interna do Docker.
- O console do Keycloak (`/auth/admin`) só abre a partir de `CV_ADMIN_CIDRS`.
- O `setup.sh` gera senhas aleatórias e grava o `.env` com permissão 600. Nunca versione o `.env`.
- A chave privada do cofre fica no volume `vault-private`, montado só por quem precisa ler
  credenciais (workers, Fase 1). A API monta apenas a chave pública.
