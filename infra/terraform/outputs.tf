output "rds_endpoint" {
  value       = module.rds.endpoint
  description = "RDS Postgres hostname."
}

output "rds_port" {
  value       = module.rds.port
  description = "RDS Postgres port."
}

output "redis_cache_endpoint" {
  value       = module.redis_cache.primary_endpoint
  description = "Cache/pubsub Redis primary endpoint."
}

output "redis_broker_endpoint" {
  value       = module.redis_broker.primary_endpoint
  description = "Celery broker Redis primary endpoint."
}

output "alb_dns_name" {
  value       = module.ecs.alb_dns_name
  description = "Public app ALB DNS name."
}

output "gateway_nlb_dns_name" {
  value       = module.gateway.nlb_dns_name
  description = "Public gateway NLB DNS name (websocket clients)."
}

output "ecs_cluster_name" {
  value       = module.ecs.cluster_name
  description = "App ECS cluster name."
}

output "gateway_cluster_name" {
  value       = module.gateway.cluster_name
  description = "Gateway ECS cluster name."
}

output "cloudfront_domain_name" {
  value       = module.cdn.distribution_domain_name
  description = "CloudFront distribution domain name."
}

output "unleash_url" {
  value       = module.unleash.url
  description = "Internal Unleash base URL."
}

output "alerts_sns_topic_arn" {
  value       = module.observability.sns_topic_arn
  description = "Alerts SNS topic ARN."
}

output "codedeploy_deployment_group" {
  value       = module.deploy.deployment_group_name
  description = "CodeDeploy deployment group for the app."
}
