# ------------------------------------------------------------------------------
# One ElastiCache Redis replication group. Instantiated twice by the root:
#   - cache/pubsub (allkeys-lru, no AOF) — also Centrifugo's engine
#   - Celery broker (noeviction + AOF) — enqueues must not be silently evicted
# (RFC-001 §6.4).
# ------------------------------------------------------------------------------

resource "aws_elasticache_subnet_group" "this" {
  name       = "relay-${var.environment}-${var.name}"
  subnet_ids = var.subnet_ids
}

resource "aws_elasticache_parameter_group" "this" {
  name   = "relay-${var.environment}-${var.name}"
  family = var.parameter_group_family

  parameter {
    name  = "maxmemory-policy"
    value = var.maxmemory_policy
  }

  parameter {
    name  = "appendonly"
    value = var.appendonly ? "yes" : "no"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "relay-${var.environment}-${var.name}"
  description          = "Relay ${var.name} Redis (${var.environment})."

  engine         = "redis"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = 6379

  # Single node group; replica_count replicas give AZ failover.
  num_cache_clusters         = 1 + var.replica_count
  automatic_failover_enabled = var.replica_count > 0
  multi_az_enabled           = var.replica_count > 0

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = var.security_group_ids
  parameter_group_name = aws_elasticache_parameter_group.this.name

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  snapshot_retention_limit = var.appendonly ? 7 : 0
  maintenance_window       = "sun:05:00-sun:06:00"

  tags = {
    Name = "relay-${var.environment}-${var.name}"
  }
}
