# Segredos internos da plataforma. Valores aleatórios gerados aqui; nunca commitados.
resource "random_password" "app_db" {
  length  = 40
  special = false
}

resource "random_password" "keycloak_db" {
  length  = 40
  special = false
}

resource "random_password" "bff_client" {
  length  = 48
  special = false
}

resource "random_password" "redis_auth" {
  length  = 48
  special = false
}

resource "random_password" "keycloak_admin" {
  length  = 32
  special = false
}

locals {
  internal_secrets = {
    "app-db-password"      = random_password.app_db.result
    "keycloak-db-password" = random_password.keycloak_db.result
    "bff-client-secret"    = random_password.bff_client.result
    "redis-auth-token"     = random_password.redis_auth.result
    "keycloak-admin"       = random_password.keycloak_admin.result
  }
}

resource "aws_secretsmanager_secret" "internal" {
  for_each                = local.internal_secrets
  name                    = "${local.name}/internal/${each.key}"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = local.is_prod ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "internal" {
  for_each      = local.internal_secrets
  secret_id     = aws_secretsmanager_secret.internal[each.key].id
  secret_string = each.value
}

# Prefixo onde a API grava credenciais de nuvem dos clientes (Fase 1+):
#   cloudview-<env>/cloud-credentials/<tenant_id>/<connection_id>
# Cifradas com a chave aws_kms_key.cloud_credentials (ver kms.tf).
locals {
  cloud_credentials_prefix = "${local.name}/cloud-credentials/"
  cloud_credentials_arn    = "arn:${local.partition}:secretsmanager:${var.region}:${local.account_id}:secret:${local.cloud_credentials_prefix}*"
}

# Alerta de segurança: leitura de credenciais de clientes por quem não é worker
resource "aws_cloudwatch_event_rule" "credential_read_outside_workers" {
  name        = "${local.name}-credential-read-outside-workers"
  description = "GetSecretValue em credenciais de clientes por principal diferente dos workers"
  event_pattern = jsonencode({
    source        = ["aws.secretsmanager"]
    "detail-type" = ["AWS API Call via CloudTrail"]
    detail = {
      eventName = ["GetSecretValue"]
      requestParameters = {
        secretId = [{ prefix = "arn:${local.partition}:secretsmanager:${var.region}:${local.account_id}:secret:${local.cloud_credentials_prefix}" }]
      }
      userIdentity = {
        sessionContext = {
          sessionIssuer = {
            arn = [{ "anything-but" = [aws_iam_role.worker_collect.arn, aws_iam_role.worker_actions.arn] }]
          }
        }
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "credential_read_alarm" {
  rule = aws_cloudwatch_event_rule.credential_read_outside_workers.name
  arn  = aws_sns_topic.platform_alarms.arn
}

# URLs de conexão completas (montadas após criar RDS/Redis), injetadas nas tasks como segredo
resource "aws_secretsmanager_secret" "app_database_url" {
  name                    = "${local.name}/internal/app-database-url"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = local.is_prod ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "app_database_url" {
  secret_id     = aws_secretsmanager_secret.app_database_url.id
  secret_string = "postgresql+psycopg://cloudview_app:${random_password.app_db.result}@${aws_db_instance.main.address}:5432/cloudview?sslmode=require"
}

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${local.name}/internal/redis-url"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = local.is_prod ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
}
