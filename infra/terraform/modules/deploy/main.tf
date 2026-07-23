# ------------------------------------------------------------------------------
# CodeDeploy blue/green for the app (ECS deployment controller). Traffic shifts
# 5% as a canary, holds 15 minutes to bake against the SLO-burn alarms, then the
# remainder shifts. Any SLO-burn alarm firing during the bake auto-rolls-back.
#
# Note on "5% canary 15 min then linear": CodeDeploy's ECS routing supports one
# strategy per deployment config. TimeBasedCanary = <one step then the rest>;
# TimeBasedLinear = <equal steps every N min>. We encode the required "hold a
# small canary, then complete" shape as a TimeBasedCanary at 5% / 15 min (the
# canary-then-linear intent). A pure step-ladder ("then linear") would instead
# use a TimeBasedLinear config (e.g. linear 10% every 3 min) — swap
# traffic_routing_config.type below if the ladder shape is preferred; the
# deployment group is agnostic to which config it references.
# ------------------------------------------------------------------------------

resource "aws_iam_role" "codedeploy" {
  name = "relay-${var.environment}-codedeploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codedeploy.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "codedeploy_ecs" {
  role       = aws_iam_role.codedeploy.name
  policy_arn = "arn:aws:iam::aws:policy/AWSCodeDeployRoleForECS"
}

resource "aws_codedeploy_app" "app" {
  name             = "relay-${var.environment}-app"
  compute_platform = "ECS"
}

# Custom canary: 5% of traffic, hold 15 minutes, then shift the remainder.
resource "aws_codedeploy_deployment_config" "canary_5pct_15min" {
  deployment_config_name = "relay-${var.environment}-canary-5pct-15min"
  compute_platform       = "ECS"

  traffic_routing_config {
    type = "TimeBasedCanary"

    time_based_canary {
      interval   = 15 # minutes to hold at the canary percentage
      percentage = 5  # canary traffic share
    }
  }
}

resource "aws_codedeploy_deployment_group" "app" {
  app_name               = aws_codedeploy_app.app.name
  deployment_group_name  = "relay-${var.environment}-app"
  service_role_arn       = aws_iam_role.codedeploy.arn
  deployment_config_name = aws_codedeploy_deployment_config.canary_5pct_15min.deployment_config_name

  deployment_style {
    deployment_option = "WITH_TRAFFIC_CONTROL"
    deployment_type   = "BLUE_GREEN"
  }

  ecs_service {
    cluster_name = var.ecs_cluster_name
    service_name = var.app_service_name
  }

  blue_green_deployment_config {
    deployment_ready_option {
      action_on_timeout = "CONTINUE_DEPLOYMENT"
    }

    terminate_blue_instances_on_deployment_success {
      action                           = "TERMINATE"
      termination_wait_time_in_minutes = 15
    }
  }

  load_balancer_info {
    target_group_pair_info {
      prod_traffic_route {
        listener_arns = [var.prod_listener_arn]
      }

      target_group {
        name = var.target_group_blue_name
      }

      target_group {
        name = var.target_group_green_name
      }
    }
  }

  # Auto-rollback the moment an SLO-burn alarm fires during the canary bake.
  auto_rollback_configuration {
    enabled = true
    events  = ["DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_ALARM"]
  }

  alarm_configuration {
    enabled                   = true
    ignore_poll_alarm_failure = false
    alarms                    = var.auto_rollback_alarm_names
  }
}
