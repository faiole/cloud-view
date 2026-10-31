output "app_url" {
  value = "https://${local.app_host}"
}

output "auth_url" {
  value = "https://${local.auth_host}"
}

output "alb_dns_name" {
  description = "Aponte o DNS do painel e de auth.<domínio> para cá se não usar Route 53"
  value       = aws_lb.main.dns_name
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "worker_collect_role_arn" {
  description = "Principal que as contas dos clientes confiam em CloudViewReadOnly (template de onboarding)"
  value       = aws_iam_role.worker_collect.arn
}

output "worker_actions_role_arn" {
  description = "Principal que as contas dos clientes confiam em CloudViewOperator"
  value       = aws_iam_role.worker_actions.arn
}

output "migrate_task_definition" {
  value = aws_ecs_task_definition.migrate.arn
}

output "private_subnets" {
  value = module.vpc.private_subnets
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "cloud_credentials_kms_key_arn" {
  value = aws_kms_key.cloud_credentials.arn
}
