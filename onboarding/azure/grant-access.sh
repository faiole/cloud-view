#!/usr/bin/env bash
# Cloud View — libera o acesso do app ao Azure do cliente (rodar no Cloud Shell do cliente).
#
#   ./grant-access.sh <APP_CLIENT_ID> <ESCOPO> [--actions] [--export-storage <id-da-conta-de-armazenamento>]
#
# ESCOPO: um grupo de gerenciamento (recomendado, cobre subscriptions novas)
#   /providers/Microsoft.Management/managementGroups/<id>
# ou uma subscription: /subscriptions/<id>
#
# Antes, o administrador do tenant precisa dar consentimento ao app (link mostrado no painel).
set -euo pipefail

APP_ID="${1:?informe o client id do app}"
SCOPE="${2:?informe o escopo (grupo de gerenciamento ou subscription)}"
shift 2
ACTIONS=false
STORAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --actions) ACTIONS=true ;;
    --export-storage) STORAGE="$2"; shift ;;
  esac
  shift
done

SP=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || true)
if [ -z "$SP" ]; then
  echo "Service principal do app não encontrado. Abra primeiro o link de consentimento do administrador." >&2
  exit 1
fi

assign() {
  az role assignment create --assignee-object-id "$SP" --assignee-principal-type ServicePrincipal \
    --role "$1" --scope "$2" --only-show-errors >/dev/null
  echo "ok: $1 em $2"
}

# Leitura de inventário, rede, saúde, Advisor e custos
assign "Reader" "$SCOPE"
assign "Cost Management Reader" "$SCOPE"

if [ "$ACTIONS" = true ]; then
  # Papel mínimo para reiniciar, ligar e desligar (deallocate) VMs.
  # Dica: atribua só nos grupos de recursos onde as ações devem ser permitidas.
  ROLE_NAME="Cloud View Operator"
  if ! az role definition list --name "$ROLE_NAME" --query "[0].id" -o tsv | grep -q .; then
    az role definition create --role-definition "{
      \"Name\": \"$ROLE_NAME\",
      \"Description\": \"Cloud View: reiniciar, ligar e desligar máquinas virtuais\",
      \"Actions\": [
        \"Microsoft.Compute/virtualMachines/read\",
        \"Microsoft.Compute/virtualMachines/instanceView/read\",
        \"Microsoft.Compute/virtualMachines/start/action\",
        \"Microsoft.Compute/virtualMachines/restart/action\",
        \"Microsoft.Compute/virtualMachines/deallocate/action\"
      ],
      \"AssignableScopes\": [\"$SCOPE\"]
    }" --only-show-errors >/dev/null
    echo "ok: papel '$ROLE_NAME' criado"
  fi
  assign "$ROLE_NAME" "$SCOPE"
fi

if [ -n "$STORAGE" ]; then
  # Leitura do export FOCUS (custos com tags)
  assign "Storage Blob Data Reader" "$STORAGE"
fi

echo "Pronto. Volte ao painel e clique em 'Testar' na conexão."
