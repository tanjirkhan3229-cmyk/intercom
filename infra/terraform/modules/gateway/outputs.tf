output "nlb_dns_name" {
  value       = aws_lb.gateway.dns_name
  description = "Public NLB DNS name for websocket clients."
}

output "cluster_name" {
  value       = aws_ecs_cluster.gateway.name
  description = "Gateway ECS cluster name."
}

output "cluster_arn" {
  value       = aws_ecs_cluster.gateway.arn
  description = "Gateway ECS cluster ARN."
}

output "service_name" {
  value       = aws_ecs_service.gateway.name
  description = "Gateway ECS service name."
}

output "internal_api_url" {
  value       = "https://${aws_lb.gateway.dns_name}:443"
  description = "Internal Centrifugo API base URL (used by the app tier); TLS-terminated at the NLB."
}

# Preserves the summary the former root-level stub exposed.
output "gateway_tier_summary" {
  value       = "centrifugo ${var.centrifugo_image}: ${var.gateway_node_count} x ${var.gateway_node_memory_mb}MB (${var.environment})"
  description = "Human-readable gateway tier summary."
}
