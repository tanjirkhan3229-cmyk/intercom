variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "region" {
  type        = string
  description = "AWS region (for log configuration)."
}

variable "vpc_id" {
  type        = string
  description = "VPC id (for the app target group)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Subnets to place Fargate tasks in."
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Subnets for the public ALB."
}

variable "app_sg_id" {
  type        = string
  description = "Security group for the app/worker/beat tasks."
}

variable "alb_sg_id" {
  type        = string
  description = "Security group for the ALB."
}

variable "image" {
  type        = string
  description = "Container image repo (without tag)."
}

variable "image_tag" {
  type        = string
  description = "Image tag to run."
}

# ---- App sizing / scaling ----

variable "app_cpu" {
  type = number
}

variable "app_memory" {
  type = number
}

variable "app_desired_count" {
  type = number
}

variable "app_min_count" {
  type = number
}

variable "app_max_count" {
  type = number
}

variable "worker_cpu" {
  type = number
}

variable "worker_memory" {
  type = number
}

variable "worker_desired_count" {
  type = number
}

variable "beat_cpu" {
  type = number
}

variable "beat_memory" {
  type = number
}

# ---- Runtime wiring ----

variable "database_endpoint" {
  type        = string
  description = "RDS hostname."
}

variable "database_port" {
  type        = number
  description = "RDS port."
}

variable "db_name" {
  type        = string
  description = "Database name."
}

variable "redis_cache_endpoint" {
  type        = string
  description = "Cache/pubsub Redis primary endpoint."
}

variable "redis_broker_endpoint" {
  type        = string
  description = "Broker Redis primary endpoint."
}

variable "centrifugo_api_url" {
  type        = string
  description = "Internal Centrifugo API base URL (NLB)."
}

# ---- Secrets (ARNs from the secrets module) ----

variable "jwt_signing_key_arn" {
  type = string
}

variable "centrifugo_token_arn" {
  type = string
}

variable "centrifugo_apikey_arn" {
  type = string
}

variable "ses_credentials_arn" {
  type = string
}

variable "secret_arns" {
  type        = list(string)
  description = "All secret ARNs the task exec role may read."
}
