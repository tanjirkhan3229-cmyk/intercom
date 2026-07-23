# ------------------------------------------------------------------------------
# Golden-signals observability: SLO-burn alarms (5xx rate, p95 latency), a
# queue-depth/oldest-age alarm, an SNS alert topic, and a four-signal dashboard.
# The SLO-burn alarm ARNs feed CodeDeploy auto-rollback (module deploy).
# ------------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "relay-${var.environment}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---- Application log group for structured app logs / metric filters ----

resource "aws_cloudwatch_log_group" "app_events" {
  name              = "/relay/${var.environment}/events"
  retention_in_days = 30
}

# Queue oldest-age comes from a custom metric the workers emit. A log metric
# filter turns structured "queue_oldest_age_seconds" log lines into a metric.
resource "aws_cloudwatch_log_metric_filter" "queue_oldest_age" {
  name           = "relay-${var.environment}-queue-oldest-age"
  log_group_name = aws_cloudwatch_log_group.app_events.name
  pattern        = "{ $.metric = \"queue_oldest_age_seconds\" }"

  metric_transformation {
    name          = "QueueOldestAgeSeconds"
    namespace     = "Relay/${var.environment}"
    value         = "$.value"
    default_value = "0"
  }
}

# ---- SLO-burn alarms ----

resource "aws_cloudwatch_metric_alarm" "http_5xx" {
  alarm_name          = "relay-${var.environment}-slo-5xx"
  alarm_description   = "SLO burn: elevated 5xx rate at the ALB."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = var.alarm_5xx_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "p95_latency" {
  alarm_name          = "relay-${var.environment}-slo-p95-latency"
  alarm_description   = "SLO burn: p95 target response time over budget."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = var.alarm_p95_latency_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ---- Queue depth / oldest-age alarm (backpressure) ----

resource "aws_cloudwatch_metric_alarm" "queue_oldest_age" {
  alarm_name          = "relay-${var.environment}-queue-oldest-age"
  alarm_description   = "Celery/outbox backpressure: oldest unprocessed message too old."
  namespace           = "Relay/${var.environment}"
  metric_name         = "QueueOldestAgeSeconds"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = var.alarm_queue_oldest_age_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ---- Four golden signals dashboard ----

resource "aws_cloudwatch_dashboard" "golden" {
  dashboard_name = "relay-${var.environment}-golden-signals"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6,
        properties = {
          title  = "Traffic - request count",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6,
        properties = {
          title  = "Errors - 5xx",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6,
        properties = {
          title  = "Latency - target response time (p50/p95/p99)",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p50" }],
            ["...", { stat = "p95" }],
            ["...", { stat = "p99" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6,
        properties = {
          title  = "Saturation - ECS CPU/memory + queue age",
          region = var.region,
          view   = "timeSeries",
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.app_service_name, { stat = "Average" }],
            ["AWS/ECS", "MemoryUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.app_service_name, { stat = "Average" }],
            ["Relay/${var.environment}", "QueueOldestAgeSeconds", { stat = "Maximum" }]
          ]
        }
      }
    ]
  })
}
