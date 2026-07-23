variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "ecs_cluster_name" {
  type        = string
  description = "App ECS cluster name."
}

variable "app_service_name" {
  type        = string
  description = "App ECS service name."
}

variable "prod_listener_arn" {
  type        = string
  description = "ALB production listener ARN (traffic shifting)."
}

variable "target_group_blue_name" {
  type        = string
  description = "Blue target group name."
}

variable "target_group_green_name" {
  type        = string
  description = "Green target group name."
}

variable "auto_rollback_alarm_names" {
  type        = list(string)
  description = "CloudWatch SLO-burn alarm names that trigger auto-rollback."
}
