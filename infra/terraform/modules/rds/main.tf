# ------------------------------------------------------------------------------
# RDS Postgres primary. Automated snapshots + PITR come from
# backup_retention_period > 0; multi_az is a per-environment toggle. The
# parameter group carries log_min_duration_statement=500 to mirror the dev
# compose stack (RFC-002 §9).
# ------------------------------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  name       = "relay-${var.environment}"
  subnet_ids = var.subnet_ids

  tags = {
    Name = "relay-${var.environment}"
  }
}

resource "aws_db_parameter_group" "this" {
  name        = "relay-${var.environment}-pg"
  family      = var.parameter_group_family
  description = "Relay Postgres parameters (${var.environment})."

  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }

  parameter {
    name  = "log_lock_waits"
    value = "1"
  }

  # Enforce TLS in transit: reject any non-SSL connection. The application must
  # connect with TLS (verify-full via the RDS CA bundle) — the app-side
  # connect_args change is owned by the code owner and handled separately.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.identifier}-${var.environment}"
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  # Password managed by RDS + Secrets Manager (no plaintext in state/code).
  manage_master_user_password = true

  multi_az               = var.multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = var.security_group_ids
  parameter_group_name   = aws_db_parameter_group.this.name

  # Automated snapshots + point-in-time recovery.
  backup_retention_period  = var.backup_retention
  backup_window            = "03:00-04:00"
  maintenance_window       = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "relay-${var.environment}-final"

  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
  apply_immediately            = false

  tags = {
    Name = "relay-${var.environment}"
  }
}
