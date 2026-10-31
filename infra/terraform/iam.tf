data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# --- Execution role: puxa imagens e injeta segredos internos nas tasks ------------------------
resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_base" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [for s in aws_secretsmanager_secret.internal : s.arn],
      [
        aws_secretsmanager_secret.app_database_url.arn,
        aws_secretsmanager_secret.redis_url.arn,
        aws_db_instance.main.master_user_secret[0].secret_arn,
      ],
    )
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --- API: lê o próprio banco/cache; grava (não lê) credenciais de clientes; enfileira jobs -----
resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api_task" {
  statement {
    sid = "WriteCloudCredentials"
    actions = [
      "secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret",
      "secretsmanager:DeleteSecret", "secretsmanager:TagResource", "secretsmanager:DescribeSecret",
    ]
    resources = [local.cloud_credentials_arn]
  }
  statement {
    sid       = "DenyReadCloudCredentials"
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.cloud_credentials_arn]
  }
  statement {
    sid       = "EnqueueJobs"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main["collect"].arn, aws_sqs_queue.main["actions"].arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "api_task" {
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# --- Worker de coleta: assume SOMENTE CloudViewReadOnly nas contas dos clientes ----------------
resource "aws_iam_role" "worker_collect" {
  name               = "${local.name}-worker-collect"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker_collect" {
  statement {
    sid       = "AssumeCustomerReadOnly"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:${local.partition}:iam::*:role/CloudViewReadOnly"]
  }
  statement {
    sid       = "ReadCloudCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.cloud_credentials_arn]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:SendMessage"]
    resources = [aws_sqs_queue.main["collect"].arn, aws_sqs_queue.main["events"].arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "worker_collect" {
  role   = aws_iam_role.worker_collect.id
  policy = data.aws_iam_policy_document.worker_collect.json
}

# --- Worker de ações: única identidade que assume CloudViewOperator (escrita) -----------------
resource "aws_iam_role" "worker_actions" {
  name               = "${local.name}-worker-actions"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker_actions" {
  statement {
    sid       = "AssumeCustomerOperator"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:${local.partition}:iam::*:role/CloudViewOperator"]
  }
  statement {
    sid       = "ReadCloudCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.cloud_credentials_arn]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.main["actions"].arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "worker_actions" {
  role   = aws_iam_role.worker_actions.id
  policy = data.aws_iam_policy_document.worker_actions.json
}

# --- Keycloak: sem permissões AWS ---------------------------------------------------------------
resource "aws_iam_role" "keycloak_task" {
  name               = "${local.name}-keycloak"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}
