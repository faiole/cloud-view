# Chave de dados da plataforma: RDS, Redis, SQS, logs, segredos internos
resource "aws_kms_key" "data" {
  description             = "${local.name} - dados da plataforma"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms_data.json
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

data "aws_iam_policy_document" "kms_data" {
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
  }
}

# Chave EXCLUSIVA das credenciais de nuvem dos clientes (OCI API keys, certificados Azure...).
# Só as roles dos workers podem decifrar; a API web pode apenas cifrar (gravar) novos segredos.
resource "aws_kms_key" "cloud_credentials" {
  description             = "${local.name} - credenciais de nuvem dos clientes"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms_credentials.json
}

resource "aws_kms_alias" "cloud_credentials" {
  name          = "alias/${local.name}-cloud-credentials"
  target_key_id = aws_kms_key.cloud_credentials.key_id
}

data "aws_iam_policy_document" "kms_credentials" {
  statement {
    sid = "KeyAdministration"
    # Administração da chave sem permissão de uso (Decrypt) para a conta inteira
    actions = [
      "kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*",
      "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:TagResource", "kms:UntagResource",
      "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion",
    ]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "WorkersDecrypt"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.worker_collect.arn, aws_iam_role.worker_actions.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "ApiEncryptOnly"
    actions   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.api_task.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}
