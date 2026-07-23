# ------------------------------------------------------------------------------
# Root input variables. Every variable carries a sane placeholder default so
# `terraform validate` runs without any tfvars. Environments override via the
# files in environments/*.tfvars.
# ------------------------------------------------------------------------------

variable "region" {
  type        = string
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "environment" {
  type        = string
  description = "Deployment environment (staging | production)."
  default     = "staging"
}

# ---- Networking ----

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC."
  default     = "10.20.0.0/16"
}

variable "azs" {
  type        = list(string)
  description = "Availability zones to spread subnets across."
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

# ---- RDS Postgres ----

variable "rds_instance_class" {
  type        = string
  description = "Instance class for the primary Postgres database."
  default     = "db.t4g.medium"
}

variable "rds_allocated_storage" {
  type        = number
  description = "Allocated storage (GiB) for RDS."
  default     = 50
}

variable "rds_max_allocated_storage" {
  type        = number
  description = "Storage autoscaling ceiling (GiB) for RDS."
  default     = 200
}

variable "rds_engine_version" {
  type        = string
  description = "Postgres engine version."
  default     = "16.4"
}

variable "multi_az" {
  type        = bool
  description = "Run RDS in multi-AZ (true for production)."
  default     = false
}

variable "backup_retention" {
  type        = number
  description = "RDS automated backup retention in days (short for staging, 30 for prod)."
  default     = 7
}

variable "db_name" {
  type        = string
  description = "Initial database name."
  default     = "relay"
}

variable "db_username" {
  type        = string
  description = "Master username for RDS."
  default     = "relay_admin"
}

# ---- ElastiCache Redis ----

variable "redis_node_type" {
  type        = string
  description = "ElastiCache node type for both Redis clusters."
  default     = "cache.t4g.small"
}

variable "redis_engine_version" {
  type        = string
  description = "Redis engine version."
  default     = "7.1"
}

variable "redis_cache_replicas" {
  type        = number
  description = "Read-replica count for the cache/pubsub Redis replication group."
  default     = 1
}

variable "redis_broker_replicas" {
  type        = number
  description = "Read-replica count for the Celery broker Redis replication group."
  default     = 1
}

# ---- ECS / app tier ----

variable "api_image" {
  type        = string
  description = "Container image (repo:tag) for the FastAPI app / workers / beat."
  default     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/relay-api:latest"
}

variable "image_tag" {
  type        = string
  description = "Image tag driving deploys (overridden in CI)."
  default     = "latest"
}

variable "app_cpu" {
  type        = number
  description = "Fargate CPU units for the app service task."
  default     = 512
}

variable "app_memory" {
  type        = number
  description = "Fargate memory (MiB) for the app service task."
  default     = 1024
}

variable "app_desired_count" {
  type        = number
  description = "Desired running count for the app service."
  default     = 2
}

variable "app_min_count" {
  type        = number
  description = "Autoscaling minimum for the app service."
  default     = 2
}

variable "app_max_count" {
  type        = number
  description = "Autoscaling maximum for the app service."
  default     = 10
}

variable "worker_cpu" {
  type        = number
  description = "Fargate CPU units for the Celery worker task."
  default     = 512
}

variable "worker_memory" {
  type        = number
  description = "Fargate memory (MiB) for the Celery worker task."
  default     = 1024
}

variable "worker_desired_count" {
  type        = number
  description = "Desired running count for the worker service."
  default     = 2
}

variable "beat_cpu" {
  type        = number
  description = "Fargate CPU units for the Celery beat task."
  default     = 256
}

variable "beat_memory" {
  type        = number
  description = "Fargate memory (MiB) for the Celery beat task."
  default     = 512
}

# ---- Gateway (Centrifugo) ----

variable "gateway_image" {
  type        = string
  description = "Centrifugo container image."
  default     = "centrifugo/centrifugo:v5"
}

variable "gateway_node_count" {
  type        = number
  description = "Centrifugo desired node count. RFC-001 §5.2: 6-10 at the 500k-connection envelope."
  default     = 6
}

variable "gateway_min_count" {
  type        = number
  description = "Autoscaling minimum for the gateway service."
  default     = 3
}

variable "gateway_max_count" {
  type        = number
  description = "Autoscaling maximum for the gateway service."
  default     = 12
}

variable "gateway_cpu" {
  type        = number
  description = "Fargate CPU units for a Centrifugo node."
  default     = 1024
}

variable "gateway_memory_mb" {
  type        = number
  description = "Fargate memory (MiB) per Centrifugo node. RFC-001 §5.2 sizes at ~4 GB."
  default     = 4096
}

variable "gateway_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for the public gateway NLB TLS listener on 443. A real per-env ACM ARN is REQUIRED in production; empty is a placeholder for validate-only runs."
  default     = ""
}

# ---- CDN / DNS ----

variable "widget_bucket_name" {
  type        = string
  description = "S3 bucket name for widget bundles."
  default     = "relay-widget-staging"
}

variable "cdn_domain_names" {
  type        = list(string)
  description = "Alternate domain names (CNAMEs) for the CloudFront distribution. Empty uses the default *.cloudfront.net domain."
  default     = []
}

variable "cdn_acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN (us-east-1) for the CloudFront distribution. Empty uses the default CloudFront cert."
  default     = ""
}

# ---- Unleash ----

variable "unleash_image" {
  type        = string
  description = "Unleash feature-flag server container image."
  default     = "unleashorg/unleash-server:latest"
}

variable "unleash_cpu" {
  type        = number
  description = "Fargate CPU units for Unleash."
  default     = 256
}

variable "unleash_memory" {
  type        = number
  description = "Fargate memory (MiB) for Unleash."
  default     = 512
}

variable "unleash_db_instance_class" {
  type        = string
  description = "Instance class for the Unleash backing Postgres."
  default     = "db.t4g.micro"
}

# ---- Observability ----

variable "alarm_5xx_threshold" {
  type        = number
  description = "5xx rate (count over the evaluation window) that trips the SLO-burn alarm."
  default     = 25
}

variable "alarm_p95_latency_seconds" {
  type        = number
  description = "p95 target-response-time (seconds) that trips the latency alarm."
  default     = 1.0
}

variable "alarm_queue_oldest_age_seconds" {
  type        = number
  description = "Oldest-message age (seconds) that trips the queue-depth/age alarm."
  default     = 300
}

variable "alert_email" {
  type        = string
  description = "Email subscribed to the alerts SNS topic."
  default     = "oncall@example.com"
}
