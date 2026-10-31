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

5. Edite o `.env` e configure o SMTP real (convites, redefinição de senha e alertas por e-mail;
   em produção use `SMTP_STARTTLS=true`) e, se quiser, a rede
   que pode abrir o console do Keycloak (`CV_ADMIN_CIDRS`).
6. Suba:

```bash
docker compose up -d --build
```

```bash
docker compose run --rm keycloak-seed
```

O Caddy emite o certificado HTTPS sozinho na primeira requisição.

## Conectar a AWS

1. **Identidade hub** (tela *Conexões*):
   - servidor numa EC2: anexe uma *instance profile* cuja role só tenha `sts:AssumeRole` em
     `arn:aws:iam::*:role/CloudViewReadOnly` e `.../CloudViewOperator`, e informe o ARN dela.
     Os containers acessam o metadata da EC2: ajuste o *hop limit* do IMDSv2 para 2
     (`aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2`);
   - qualquer outro lugar: crie o usuário IAM `cloudview-hub` com essa mesma permissão e cadastre a
     chave. Ela é cifrada com a chave pública do cofre; só os workers conseguem usá-la.
2. **Nova conexão**: informe a conta management (Organization) ou a conta avulsa. O painel gera o
   ExternalId e mostra os comandos do CloudFormation (e do StackSet, para a Organization).
3. Implante a stack, clique em **Testar** e depois em **Sincronizar agora**.
4. Em *Contas de nuvem*, atribua as contas descobertas às empresas.
5. (Recomendado) Na conta management, habilite o **Cost Optimization Hub** e crie um monitor no
   **Cost Anomaly Detection**. Para patches, as instâncias precisam do agente SSM e de uma
   *patch baseline*.

## Conectar o Azure

1. Registre um app **multi-tenant** no seu Entra ID e cadastre-o em *Conexões → Azure → App hub*
   (certificado recomendado). Passo a passo em `onboarding/azure/README.md`.
2. Para cada cliente: *Nova conexão Azure* com o ID do tenant, envie o link de consentimento ao
   administrador dele e rode `onboarding/azure/grant-access.sh` no Cloud Shell do cliente.
3. **Testar** → as subscriptions aparecem em *Contas de nuvem* para atribuir às empresas.

## Operação

| Tarefa | Como |
|---|---|
| Atualizar | `git pull && docker compose up -d --build` (as migrações rodam sozinhas) |
| Logs | `docker compose logs -f api worker worker-actions` |
| Mais coleta em paralelo | `docker compose up -d --scale worker=3` |
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
- A chave privada do cofre fica no volume `vault-private`, montado só pelos workers. A API monta
  apenas a chave pública.
