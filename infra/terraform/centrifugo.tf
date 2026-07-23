# Centrifugo realtime gateway — Terraform STUB (P0.4).
#
# The gateway is deliberately its own tier (RFC-001 §6.1 gateway row): memory- + connection-bound,
# a fleet apart from the API's request-latency profile, and an OOM here must not take the API down.
# Sizing (RFC-001 §5.2): ~500k concurrent websockets, ~20–50 KB/conn ⇒ 6–10 nodes (4 GB each) with
# headroom; the reconnect storm after a deploy/outage (≈8.3k handshakes/s over 60 s), not steady
# state, sizes the tier — jittered client backoff spreads it.
#
# This file is a stub: it declares the shape (variables + intended resources) so the real module
# lands in the infra hardening milestone (P0.12) without re-litigating the topology. Nothing here
# is applied yet.

variable "environment" {
  type        = string
  description = "Deployment environment (staging | production)."
}

variable "gateway_node_count" {
  type        = number
  default     = 6
  description = "Centrifugo nodes. RFC-001 §5.2: 6–10 at the 500k-connection envelope."
}

variable "gateway_node_memory_mb" {
  type    = number
  default = 4096
}

variable "centrifugo_image" {
  type    = string
  default = "centrifugo/centrifugo:v5"
}

variable "redis_engine_address" {
  type        = string
  description = "ElastiCache Redis used as Centrifugo's engine (pub/sub + presence broker)."
}

# Secrets resolve from AWS Secrets Manager at deploy time — never env-baked (RFC-001 §13).
# token_hmac_secret_key MUST match the API's CENTRIFUGO_TOKEN_SECRET so minted JWTs verify.
# data "aws_secretsmanager_secret_version" "centrifugo_token_secret" { ... }
# data "aws_secretsmanager_secret_version" "centrifugo_api_key" { ... }

# TODO(P0.12): ECS service (Fargate) or ASG of gateway nodes behind an NLB with websocket-aware
# health checks; sticky-less (any node serves any connection — RFC-001 §9), overload sheds *new*
# connections first. Autoscale on connection count + memory, not CPU.
#
# resource "aws_ecs_service" "centrifugo" {
#   name            = "centrifugo-${var.environment}"
#   desired_count   = var.gateway_node_count
#   ...
# }

output "gateway_tier_summary" {
  value = "centrifugo ${var.centrifugo_image}: ${var.gateway_node_count} x ${var.gateway_node_memory_mb}MB (${var.environment}) — STUB, not yet applied"
}
