variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "region" {
  type        = string
  description = "AWS region (dashboard widgets)."
}

variable "alb_arn_suffix" {
  type        = string
  description = "ALB ARN suffix for CloudWatch dimensions."
}

variable "target_group_arn_suffix" {
  type        = string
  description = "App target group ARN suffix for CloudWatch dimensions."
}

variable "ecs_cluster_name" {
  type        = string
  description = "App ECS cluster name."
}

variable "app_service_name" {
  type        = string
  description = "App ECS service name."
}

variable "alarm_5xx_threshold" {
  type        = number
  description = "5xx count over the evaluation window that trips the SLO-burn alarm."
}

variable "alarm_p95_latency_seconds" {
  type        = number
  description = "p95 target-response-time (seconds) that trips the latency alarm."
}

variable "alarm_queue_oldest_age_seconds" {
  type        = number
  description = "Oldest-message age (seconds) tripping the queue-age alarm."
}

variable "alert_email" {
  type        = string
  description = "Email subscribed to the alerts SNS topic."
}
