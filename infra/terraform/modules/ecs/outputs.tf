output "cluster_name" {
  value       = aws_ecs_cluster.this.name
  description = "ECS cluster name."
}

output "cluster_arn" {
  value       = aws_ecs_cluster.this.arn
  description = "ECS cluster ARN."
}

output "app_service_name" {
  value       = aws_ecs_service.app.name
  description = "App ECS service name."
}

output "alb_dns_name" {
  value       = aws_lb.app.dns_name
  description = "Public ALB DNS name."
}

output "alb_arn_suffix" {
  value       = aws_lb.app.arn_suffix
  description = "ALB ARN suffix (for CloudWatch dimensions)."
}

output "alb_listener_arn" {
  value       = aws_lb_listener.app.arn
  description = "ALB production listener ARN (CodeDeploy traffic shifting)."
}

output "target_group_blue_name" {
  value       = aws_lb_target_group.app_blue.name
  description = "Blue target group name."
}

output "target_group_green_name" {
  value       = aws_lb_target_group.app_green.name
  description = "Green target group name."
}

output "target_group_blue_arn_suffix" {
  value       = aws_lb_target_group.app_blue.arn_suffix
  description = "Blue target group ARN suffix (for CloudWatch dimensions)."
}
