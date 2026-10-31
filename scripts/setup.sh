#!/usr/bin/env bash
# Gera o .env com segredos aleatórios. Uso:
#   ./scripts/setup.sh                       → local, http://localhost:8080
#   ./scripts/setup.sh painel.empresa.com    → servidor com HTTPS automático (Let's Encrypt)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  echo ".env já existe; apague-o se quiser gerar de novo (os dados atuais dependem dessas senhas)."
  exit 1
fi

rand() { openssl rand -hex 24; }
DOMAIN="${1:-}"

if [ -z "$DOMAIN" ]; then
  SITE="http://"; PUBLIC="http://localhost:8080"; HTTP_PORT=8080; HTTPS_PORT=8443
  SECURE=false; ENV=dev; PROFILES=dev
else
  SITE="$DOMAIN"; PUBLIC="https://$DOMAIN"; HTTP_PORT=80; HTTPS_PORT=443
  SECURE=true; ENV=prod; PROFILES=
fi

cat > .env <<ENV
# Gerado por scripts/setup.sh em $(date -u +%Y-%m-%dT%H:%MZ). NÃO versionar.
COMPOSE_PROFILES=$PROFILES
CV_ENV=$ENV
CV_SITE_ADDRESS=$SITE
CV_PUBLIC_URL=$PUBLIC
CV_HTTP_PORT=$HTTP_PORT
CV_HTTPS_PORT=$HTTPS_PORT
CV_COOKIE_SECURE=$SECURE
# Redes que podem abrir o console do Keycloak (/auth/admin). Ex.: "200.1.2.0/24 10.0.0.0/8"
CV_ADMIN_CIDRS=private_ranges

POSTGRES_PASSWORD=$(rand)
OWNER_DB_PASSWORD=$(rand)
APP_DB_PASSWORD=$(rand)
KEYCLOAK_DB_PASSWORD=$(rand)
REDIS_PASSWORD=$(rand)
KC_BOOTSTRAP_ADMIN_PASSWORD=$(rand)
KC_BFF_CLIENT_SECRET=$(rand)
KC_ADMIN_API_CLIENT_SECRET=$(rand)
DOCUMENTS_KEY=$(openssl rand -base64 32)

FIRST_ADMIN_EMAIL=admin@cloudview.local
FIRST_ADMIN_PASSWORD=$(openssl rand -base64 12)!Aa1

# E-mail (convites e redefinição de senha). Local: mailpit em http://localhost:8025
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_FROM=nao-responda@cloudview.local
SMTP_STARTTLS=false
SMTP_AUTH=false
SMTP_USER=
SMTP_PASSWORD=

# Documentos: "local" (volume Docker, cifrado com DOCUMENTS_KEY) ou "s3" (qualquer S3 compatível)
CV_STORAGE_BACKEND=local
CV_DOCUMENTS_BUCKET=
CV_S3_ENDPOINT_URL=
ENV
chmod 600 .env
echo "Pronto: .env criado para $PUBLIC"
echo "Primeiro administrador: $(grep FIRST_ADMIN_EMAIL .env | cut -d= -f2) / $(grep FIRST_ADMIN_PASSWORD .env | cut -d= -f2-)"
