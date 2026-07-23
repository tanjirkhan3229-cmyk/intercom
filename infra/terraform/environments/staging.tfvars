# Staging: single-AZ, small instances, short backup retention.
region      = "us-east-1"
environment = "staging"

vpc_cidr = "10.20.0.0/16"
azs      = ["us-east-1a", "us-east-1b"]

# RDS — single-AZ, small, 7-day backups.
rds_instance_class        = "db.t4g.medium"
rds_allocated_storage     = 50
rds_max_allocated_storage = 200
rds_engine_version        = "16.4"
multi_az                  = false
backup_retention          = 7
db_name                   = "relay"
db_username               = "relay_admin"

# Redis — small nodes, single replica.
redis_node_type       = "cache.t4g.small"
redis_engine_version  = "7.1"
redis_cache_replicas  = 1
redis_broker_replicas = 1

# App tier — modest.
api_image            = "123456789012.dkr.ecr.us-east-1.amazonaws.com/relay-api:staging"
image_tag            = "staging"
app_cpu              = 512
app_memory           = 1024
app_desired_count    = 2
app_min_count        = 2
app_max_count        = 6
worker_cpu           = 512
worker_memory        = 1024
worker_desired_count = 2
beat_cpu             = 256
beat_memory          = 512

# Gateway — small fleet at staging scale.
gateway_image      = "centrifugo/centrifugo:v5"
gateway_node_count = 2
gateway_min_count  = 2
gateway_max_count  = 4
gateway_cpu        = 512
gateway_memory_mb  = 2048
# Placeholder ACM ARN — an operator must substitute the real staging cert before apply (a TLS
# listener rejects an empty ARN; the gateway listener has a precondition enforcing this).
gateway_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/REPLACE-ME-GATEWAY-STAGING"

# CDN.
widget_bucket_name      = "relay-widget-staging"
cdn_domain_names        = []
cdn_acm_certificate_arn = ""

# Unleash.
unleash_image             = "unleashorg/unleash-server:latest"
unleash_cpu               = 256
unleash_memory            = 512
unleash_db_instance_class = "db.t4g.micro"

# Observability — looser thresholds for a low-traffic env.
alarm_5xx_threshold            = 15
alarm_p95_latency_seconds      = 1.5
alarm_queue_oldest_age_seconds = 300
alert_email                    = "staging-oncall@example.com"
