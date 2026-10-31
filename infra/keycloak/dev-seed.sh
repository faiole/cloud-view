#!/bin/bash
# Cria o primeiro administrador da plataforma no realm cloudview.
# Uso: docker compose run --rm keycloak-seed   (variáveis FIRST_ADMIN_EMAIL / FIRST_ADMIN_PASSWORD)
# No primeiro login o Keycloak obriga a trocar a senha e cadastrar o TOTP.
set -euo pipefail
KCADM=/opt/keycloak/bin/kcadm.sh
URL=${KC_URL:-http://keycloak:8080/auth}

for i in $(seq 1 60); do
  $KCADM config credentials --server "$URL" --realm master \
    --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1 && break
  echo "aguardando keycloak... ($i)"; sleep 2
done

EMAIL=${FIRST_ADMIN_EMAIL:-admin@cloudview.local}
if [ -z "$($KCADM get users -r cloudview -q email="$EMAIL" --fields id --format csv --noquotes)" ]; then
  $KCADM create users -r cloudview -s username="$EMAIL" -s email="$EMAIL" -s emailVerified=true \
    -s enabled=true -s firstName=Admin -s lastName=Dev -s 'requiredActions=["UPDATE_PASSWORD","CONFIGURE_TOTP"]'
  $KCADM set-password -r cloudview --username "$EMAIL" --new-password "${FIRST_ADMIN_PASSWORD:-ChangeMe!12345}" --temporary
  $KCADM add-roles -r cloudview --uusername "$EMAIL" --rolename platform-admin
  echo "usuário $EMAIL criado"
else
  echo "usuário $EMAIL já existe"
fi
