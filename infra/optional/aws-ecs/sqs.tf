# Filas dos workers (Fase 1+): coleta, execução de ações e eventos vindos das nuvens
locals {
  queues = {
    collect = { visibility = 900 }
    actions = { visibility = 300 }
    events  = { visibility = 120 }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each                  = local.queues
  name                      = "${local.name}-${each.key}-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.data.id
}

resource "aws_sqs_queue" "main" {
  for_each                   = local.queues
  name                       = "${local.name}-${each.key}"
  visibility_timeout_seconds = each.value.visibility
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.data.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  for_each            = local.queues
  alarm_name          = "${local.name}-${each.key}-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq[each.key].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.platform_alarms.arn]
}
