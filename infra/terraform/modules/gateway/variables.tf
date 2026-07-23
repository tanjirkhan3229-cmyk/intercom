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
  description = "VPC id (for the NLB target group)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Subnets for the Centrifugo Fargate tasks."
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Subnets for the internet-facing NLB."
}

variable "gateway_sg_id" {
  type        = string
  description = "Security group for the Centrifugo tasks."
}

# ---- Folded in from the centrifugo.tf stub (RFC-001 §5.2, §6.1, §9) ----

variable "centrifugo_image" {
  type        = string
  description = "Centrifugo container image."
  default     = "centrifugo/centrifugo:v5"
}

variable "gateway_node_count" {
  type        = number
  description = "Desired Centrifugo node count. RFC-001 §5.2: 6-10 at the 500k-connection envelope."
  default     = 6
}

variable "gateway_min_count" {
  type        = number
  description = "Autoscaling minimum."
  default     = 3
}

variable "gateway_max_count" {
  type        = number
  description = "Autoscaling maximum."
  default     = 12
}

variable "gateway_cpu" {
  type        = number
  description = "Fargate CPU units per node."
  default     = 1024
}

variable "gateway_node_memory_mb" {
  type        = number
  description = "Fargate memory (MiB) per node. RFC-001 §5.2 sizes at ~4 GB."
  default     = 4096
}

variable "redis_engine_address" {
  type        = string
  description = "ElastiCache Redis used as Centrifugo's engine (pub/sub + presence broker)."
}

# ---- Secrets (ARNs from the secrets module) — never env-baked (RFC-001 §13) ----

variable "centrifugo_token_arn" {
  type        = string
  description = "Secrets Manager ARN of the Centrifugo token HMAC secret. MUST match the API's CENTRIFUGO_TOKEN_SECRET."
}

variable "centrifugo_apikey_arn" {
  type        = string
  description = "Secrets Manager ARN of the Centrifugo API key."
}

variable "secret_arns" {
  type        = list(string)
  description = "Secret ARNs the task exec role may read."
}

variable "gateway_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for the public NLB TLS listener on 443. A real per-env ACM ARN is REQUIRED in production; empty is a placeholder for validate-only runs."
  default     = ""
}
