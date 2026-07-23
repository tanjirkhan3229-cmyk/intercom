# ------------------------------------------------------------------------------
# ECS cluster + the three app-tier runtime shapes (app / worker / beat) on
# Fargate (RFC-001 §6.1). The app sits behind a public ALB; workers and beat
# have no inbound. Secrets are injected from Secrets Manager ARNs, never baked.
# ------------------------------------------------------------------------------

locals {
  full_image = "${var.image}:${var.image_tag}"

  # Non-secret environment shared by every shape.
  common_env = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "LOG_LEVEL", value = "INFO" },
    { name = "DATABASE_URL", value = "postgresql+asyncpg://${var.db_name}@${var.database_endpoint}:${var.database_port}/${var.db_name}" },
    { name = "REDIS_CACHE_URL", value = "rediss://${var.redis_cache_endpoint}:6379/0" },
    { name = "REDIS_BROKER_URL", value = "rediss://${var.redis_broker_endpoint}:6379/0" },
    { name = "CENTRIFUGO_API_URL", value = var.centrifugo_api_url },
  ]

  # Secrets pulled from Secrets Manager by the exec role at container start.
  common_secrets = [
    { name = "JWT_SIGNING_KEY", valueFrom = var.jwt_signing_key_arn },
    { name = "CENTRIFUGO_TOKEN_SECRET", valueFrom = var.centrifugo_token_arn },
    { name = "CENTRIFUGO_API_KEY", valueFrom = var.centrifugo_apikey_arn },
    { name = "SES_CREDENTIALS", valueFrom = var.ses_credentials_arn },
  ]
}

resource "aws_ecs_cluster" "this" {
  name = "relay-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ---- Log groups ----

resource "aws_cloudwatch_log_group" "app" {
  name              = "/relay/${var.environment}/app"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/relay/${var.environment}/worker"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "beat" {
  name              = "/relay/${var.environment}/beat"
  retention_in_days = 30
}

# ------------------------------------------------------------------------------
# app (FastAPI, behind the ALB)
# ------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "app" {
  family                   = "relay-${var.environment}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.app_cpu
  memory                   = var.app_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "app"
      image     = local.full_image
      essential = true
      command   = ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = local.common_env
      secrets     = local.common_secrets
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)\""]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "app"
        }
      }
    }
  ])
}

resource "aws_lb" "app" {
  name               = "relay-${var.environment}-app"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids

  tags = {
    Name = "relay-${var.environment}-app"
  }
}

resource "aws_lb_target_group" "app_blue" {
  name        = "relay-${var.environment}-app-blue"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }

  tags = { Name = "relay-${var.environment}-app-blue" }
}

# Green target group for CodeDeploy blue/green traffic shifting.
resource "aws_lb_target_group" "app_green" {
  name        = "relay-${var.environment}-app-green"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }

  tags = { Name = "relay-${var.environment}-app-green" }
}

resource "aws_lb_listener" "app" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app_blue.arn
  }

  # CodeDeploy manages the listener default action during a deploy.
  lifecycle {
    ignore_changes = [default_action]
  }
}

resource "aws_ecs_service" "app" {
  name            = "relay-${var.environment}-app"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.app_desired_count
  launch_type     = "FARGATE"

  deployment_controller {
    type = "CODE_DEPLOY"
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app_blue.arn
    container_name   = "app"
    container_port   = 8000
  }

  # CodeDeploy swaps task_definition + target group each release.
  lifecycle {
    ignore_changes = [task_definition, load_balancer, desired_count]
  }
}

# ---- App autoscaling: request count + CPU target tracking ----

resource "aws_appautoscaling_target" "app" {
  max_capacity       = var.app_max_count
  min_capacity       = var.app_min_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "app_cpu" {
  name               = "relay-${var.environment}-app-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 60
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_policy" "app_requests" {
  name               = "relay-${var.environment}-app-requests"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.app.arn_suffix}/${aws_lb_target_group.app_blue.arn_suffix}"
    }
    target_value       = 1000
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

# ------------------------------------------------------------------------------
# worker (Celery, segregated queues; no inbound)
# ------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "relay-${var.environment}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.full_image
      essential = true
      command = [
        "celery", "-A", "relay.worker.celery_app", "worker",
        "-Q", "interactive,ingest,send.email,send.channels,webhooks,analytics,housekeeping,ai.interactive,ai.batch",
        "--concurrency", "4", "-l", "INFO"
      ]
      environment = local.common_env
      secrets     = local.common_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "worker" {
  name            = "relay-${var.environment}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }
}

# ------------------------------------------------------------------------------
# beat (single scheduler; exactly one task)
# ------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "beat" {
  family                   = "relay-${var.environment}-beat"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.beat_cpu
  memory                   = var.beat_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name        = "beat"
      image       = local.full_image
      essential   = true
      command     = ["celery", "-A", "relay.worker.celery_app", "beat", "-l", "INFO"]
      environment = local.common_env
      secrets     = local.common_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.beat.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "beat"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "beat" {
  name            = "relay-${var.environment}-beat"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.beat.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # Beat is a singleton scheduler: never run two.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = false
  }
}
