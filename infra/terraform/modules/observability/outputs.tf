output "sns_topic_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "Alerts SNS topic ARN."
}

output "slo_burn_alarm_arns" {
  value = [
    aws_cloudwatch_metric_alarm.http_5xx.arn,
    aws_cloudwatch_metric_alarm.p95_latency.arn,
  ]
  description = "SLO-burn alarm ARNs used by CodeDeploy auto-rollback."
}

output "slo_burn_alarm_names" {
  value = [
    aws_cloudwatch_metric_alarm.http_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.p95_latency.alarm_name,
  ]
  description = "SLO-burn alarm names (CodeDeploy references alarms by name)."
}

output "queue_alarm_arn" {
  value       = aws_cloudwatch_metric_alarm.queue_oldest_age.arn
  description = "Queue oldest-age alarm ARN."
}
