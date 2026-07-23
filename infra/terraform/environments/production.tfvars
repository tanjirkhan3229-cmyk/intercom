# Production: multi-AZ, larger instances, 30-day backup retention.
region      = "us-east-1"
environment = "production"

vpc_cidr = "10.30.0.0/16"
azs      = ["us-east-1a", "us-east-1b", "us-east-1c"]

# RDS — multi-AZ, larger, 30-day backups + PITR.
rds_instance_class        = "db.r6g.xlarge"
rds_allocated_storage     = 200
rds_max_allocated_storage = 2000
rds_engine_version        = "16.4"
multi_az                  = true
backup_retention          = 30
db_name                   = "relay"
db_username               = "relay_admin"

# Redis — larger nodes, replicas for failover.
redis_node_type       = "cache.r6g.large"
redis_engine_version  = "7.1"
redis_cache_replicas  = 2
redis_broker_replicas = 2

# App tier — production sizing.
api_image            = "123456789012.dkr.ecr.us-east-1.amazonaws.com/relay-api:prod"
image_tag            = "prod"
app_cpu              = 1024
app_memory           = 2048
app_desired_count    = 4
app_min_count        = 4
app_max_count        = 20
worker_cpu           = 1024
worker_memory        = 2048
worker_desired_count = 4
beat_cpu             = 256
beat_memory          = 512

# Gateway — 500k-connection envelope (RFC-001 §5.2): 6-10 x 4 GB nodes.
gateway_image           = "centrifugo/centrifugo:v5"
gateway_node_count      = 6
gateway_min_count       = 4
gateway_max_count       = 12
gateway_cpu             = 1024
gateway_memory_mb       = 4096
gateway_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/REPLACE-ME-GATEWAY"

# CDN.
widget_bucket_name      = "relay-widget-production"
cdn_domain_names        = ["cdn.relay.example.com"]
cdn_acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/REPLACE-ME"

# Unleash.
unleash_image             = "unleashorg/unleash-server:latest"
unleash_cpu               = 512
unleash_memory            = 1024
unleash_db_instance_class = "db.t4g.small"

# Observability — tight SLO thresholds.
alarm_5xx_threshold            = 25
alarm_p95_latency_seconds      = 1.0
alarm_queue_oldest_age_seconds = 180
alert_email                    = "oncall@example.com"
