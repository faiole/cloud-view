resource "aws_security_group" "db" {
  name_prefix = "${local.name}-db-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_parameter_group" "pg16" {
  name_prefix = "${local.name}-pg16-"
  family      = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }

  # pg_partman/pg_cron para particionar custos e métricas (TimescaleDB não existe no RDS)
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pg_cron"
    apply_method = "pending-reboot"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.db_instance_class

  allocated_storage     = 50
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.data.arn

  db_name                       = "cloudview"
  username                      = "cloudview_owner"
  manage_master_user_password   = true # senha gerada e rotacionada pelo RDS no Secrets Manager
  master_user_secret_kms_key_id = aws_kms_key.data.key_id

  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name
  multi_az               = local.is_prod
  publicly_accessible    = false

  backup_retention_period             = local.is_prod ? 30 : 7
  backup_window                       = "05:00-06:00"
  maintenance_window                  = "sun:06:30-sun:07:30"
  deletion_protection                 = local.is_prod
  skip_final_snapshot                 = !local.is_prod
  final_snapshot_identifier           = local.is_prod ? "${local.name}-final" : null
  copy_tags_to_snapshot               = true
  auto_minor_version_upgrade          = true
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.data.arn
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  iam_database_authentication_enabled = true
}
