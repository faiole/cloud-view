resource "aws_security_group" "app" {
  name_prefix = "${local.name}-app-"
  description = "Tasks ECS (web, api, keycloak, workers)"
  vpc_id      = module.vpc.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "from_alb" {
  for_each                     = toset(["3000", "8000", "8080", "9000"])
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  ip_protocol                  = "tcp"
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "svc" {
  for_each          = toset(["web", "api", "keycloak", "migrate"])
  name              = "/ecs/${local.name}/${each.value}"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.data.arn
}

locals {
  image = { for k, r in aws_ecr_repository.this : k => "${r.repository_url}:${var.image_tag}" }

  log_config = { for k, g in aws_cloudwatch_log_group.svc : k => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = g.name
      awslogs-region        = var.region
      awslogs-stream-prefix = k
    }
  } }

  secret = { for k, s in aws_secretsmanager_secret.internal : k => s.arn }

  api_env = [
    { name = "CV_ENV", value = var.env },
    { name = "CV_PUBLIC_URL", value = "https://${local.app_host}" },
    { name = "CV_OIDC_ISSUER", value = "https://${local.auth_host}/realms/cloudview" },
    { name = "CV_OIDC_CLIENT_ID", value = "cloudview-bff" },
    { name = "CV_COOKIE_SECURE", value = "true" },
  ]
  api_secrets = [
    { name = "CV_DATABASE_URL", valueFrom = aws_secretsmanager_secret.app_database_url.arn },
    { name = "CV_REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
    { name = "CV_OIDC_CLIENT_SECRET", valueFrom = local.secret["bff-client-secret"] },
  ]
  common_container = {
    essential              = true
    readonlyRootFilesystem = true
    linuxParameters        = { initProcessEnabled = true }
  }
}

# --- Web ----------------------------------------------------------------------------------------
resource "aws_ecs_task_definition" "web" {
  family                   = "${local.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([merge(local.common_container, {
    name                   = "web"
    image                  = local.image["web"]
    readonlyRootFilesystem = false # Next.js grava cache em .next/cache
    portMappings           = [{ containerPort = 3000 }]
    logConfiguration       = local.log_config["web"]
  })])
}

# --- API ----------------------------------------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([merge(local.common_container, {
    name             = "api"
    image            = local.image["api"]
    portMappings     = [{ containerPort = 8000 }]
    environment      = local.api_env
    secrets          = local.api_secrets
    logConfiguration = local.log_config["api"]
  })])
}

# --- Migrações (task avulsa, executada pelo pipeline antes do deploy) -------------------------
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([merge(local.common_container, {
    name    = "migrate"
    image   = local.image["api"]
    command = ["python", "-m", "app.bootstrap"]
    environment = [
      { name = "DB_HOST", value = aws_db_instance.main.address },
      { name = "DB_NAME", value = "cloudview" },
      { name = "DB_MANAGE_ROLES", value = "true" },
    ]
    secrets = [
      { name = "DB_OWNER_USER", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:username::" },
      { name = "DB_OWNER_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
      { name = "APP_DB_PASSWORD", valueFrom = local.secret["app-db-password"] },
      { name = "KEYCLOAK_DB_PASSWORD", valueFrom = local.secret["keycloak-db-password"] },
    ]
    logConfiguration = local.log_config["migrate"]
  })])
}

# --- Keycloak -----------------------------------------------------------------------------------
resource "aws_ecs_task_definition" "keycloak" {
  family                   = "${local.name}-keycloak"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.keycloak_task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([merge(local.common_container, {
    name                   = "keycloak"
    image                  = local.image["keycloak"]
    readonlyRootFilesystem = false
    portMappings           = [{ containerPort = 8080 }, { containerPort = 9000 }]
    environment = [
      { name = "KC_DB_URL", value = "jdbc:postgresql://${aws_db_instance.main.address}:5432/keycloak?sslmode=require" },
      { name = "KC_DB_USERNAME", value = "keycloak" },
      { name = "KC_HOSTNAME", value = "https://${local.auth_host}" },
      { name = "KC_PROXY_HEADERS", value = "xforwarded" },
      { name = "KC_HTTP_ENABLED", value = "true" },
      { name = "KC_CACHE", value = "ispn" },
      { name = "KC_CACHE_STACK", value = "jdbc-ping" },
      { name = "CV_PUBLIC_URL", value = "https://${local.app_host}" },
      { name = "KC_BOOTSTRAP_ADMIN_USERNAME", value = "kcadmin" },
    ]
    secrets = [
      { name = "KC_DB_PASSWORD", valueFrom = local.secret["keycloak-db-password"] },
      { name = "KC_BFF_CLIENT_SECRET", valueFrom = local.secret["bff-client-secret"] },
      { name = "KC_BOOTSTRAP_ADMIN_PASSWORD", valueFrom = local.secret["keycloak-admin"] },
    ]
    logConfiguration = local.log_config["keycloak"]
  })])
}

# --- Serviços -----------------------------------------------------------------------------------
locals {
  services = {
    web      = { task = aws_ecs_task_definition.web.arn, count = var.web_desired_count, port = 3000 }
    api      = { task = aws_ecs_task_definition.api.arn, count = var.api_desired_count, port = 8000 }
    keycloak = { task = aws_ecs_task_definition.keycloak.arn, count = var.keycloak_desired_count, port = 8080 }
  }
}

resource "aws_ecs_service" "this" {
  for_each        = local.services
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = each.value.task
  desired_count   = each.value.count
  launch_type     = "FARGATE"

  enable_execute_command             = false
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = each.key == "keycloak" ? 180 : 30

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = module.vpc.private_subnets
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this[each.key].arn
    container_name   = each.key
    container_port   = each.value.port
  }

  depends_on = [aws_lb_listener.https]
}

# Keycloak em cluster: as tasks se descobrem via JDBC_PING e trocam cache na porta 7800
resource "aws_vpc_security_group_ingress_rule" "keycloak_cluster" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 7800
  to_port                      = 7800
  ip_protocol                  = "tcp"
}

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.this["api"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_desired_count
  max_capacity       = var.api_desired_count * 4
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  policy_type        = "TargetTrackingScaling"

  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
