# ------------------------------------------------------------------------------
# Centrifugo realtime gateway (RFC-001 §6.1 gateway row). Its own Fargate
# service behind a network load balancer for raw websockets: sticky-less (any
# node serves any connection, RFC-001 §9), memory- and connection-bound rather
# than CPU-bound. Secrets resolve from Secrets Manager at start, never env-baked
# (RFC-001 §13). token_hmac_secret_key MUST match the API's CENTRIFUGO_TOKEN_SECRET
# so minted JWTs verify.
#
# Sizing (RFC-001 §5.2): ~500k concurrent websockets, ~20-50 KB/conn ⇒ 6-10 nodes
# (4 GB each). The reconnect storm after a deploy/outage (~8.3k handshakes/s over
# 60s), not steady state, sizes the tier; jittered client backoff spreads it.
# Folds in the former root-level centrifugo.tf stub.
# ------------------------------------------------------------------------------

data "aws_iam_policy_document" "gw_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "relay-${var.environment}-gw-exec"
  assume_role_policy = data.aws_iam_policy_document.gw_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "gw_secrets_read" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secret_arns
  }
}

resource "aws_iam_role_policy" "gw_secrets" {
  name   = "relay-${var.environment}-gw-secrets-read"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.gw_secrets_read.json
}

resource "aws_iam_role" "task" {
  name               = "relay-${var.environment}-gw-task"
  assume_role_policy = data.aws_iam_policy_document.gw_assume.json
}

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/relay/${var.environment}/gateway"
  retention_in_days = 30
}

# ---- ECS cluster dedicated to the gateway tier (OOM here must not touch the API) ----

resource "aws_ecs_cluster" "gateway" {
  name = "relay-${var.environment}-gateway"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = "relay-${var.environment}-gateway"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.gateway_cpu
  memory                   = var.gateway_node_memory_mb
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "centrifugo"
      image     = var.centrifugo_image
      essential = true
      command   = ["centrifugo", "--health", "--engine", "redis"]
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = [
        { name = "CENTRIFUGO_ENGINE", value = "redis" },
        { name = "CENTRIFUGO_REDIS_ADDRESS", value = "rediss://${var.redis_engine_address}:6379" },
        { name = "CENTRIFUGO_HEALTH", value = "true" },
        # Sticky-less: any node serves any connection (RFC-001 §9).
        { name = "CENTRIFUGO_ALLOW_ANONYMOUS_CONNECT_WITHOUT_TOKEN", value = "false" },
      ]
      secrets = [
        { name = "CENTRIFUGO_TOKEN_HMAC_SECRET_KEY", valueFrom = var.centrifugo_token_arn },
        { name = "CENTRIFUGO_SUBSCRIPTION_TOKEN_HMAC_SECRET_KEY", valueFrom = var.centrifugo_token_arn },
        { name = "CENTRIFUGO_API_KEY", valueFrom = var.centrifugo_apikey_arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.gateway.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "centrifugo"
        }
      }
    }
  ])
}

# ---- NLB (TLS termination for websockets; no security group, preserves client IP) ----

resource "aws_lb" "gateway" {
  name                             = "relay-${var.environment}-gw"
  internal                         = false
  load_balancer_type               = "network"
  subnets                          = var.public_subnet_ids
  enable_cross_zone_load_balancing = true

  tags = {
    Name = "relay-${var.environment}-gateway"
  }
}

resource "aws_lb_target_group" "gateway" {
  name        = "relay-${var.environment}-gw"
  port        = 8000
  protocol    = "TCP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  # Websocket-aware health check: probe Centrifugo's HTTP /health endpoint.
  health_check {
    protocol            = "HTTP"
    path                = "/health"
    port                = "8000"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    interval            = 15
  }

  # Sticky-less: no stickiness block; NLB hashes flows evenly across nodes.
  tags = {
    Name = "relay-${var.environment}-gateway"
  }
}

# Public 443 listener terminates TLS at the NLB (wss:// end to end, and the app's
# CENTRIFUGO_API_KEY never crosses the public LB in cleartext), then forwards to
# the TCP target group. A real per-env ACM cert ARN is REQUIRED via
# var.gateway_certificate_arn — the empty default is only for validate-only runs.
resource "aws_lb_listener" "gateway" {
  load_balancer_arn = aws_lb.gateway.arn
  port              = 443
  protocol          = "TLS"
  certificate_arn   = var.gateway_certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }

  # A TLS listener needs a real ACM cert; an empty ARN makes `apply` fail with a cryptic ELBv2
  # error. Surface it as a clear message at plan time (this is a no-op for `validate`).
  lifecycle {
    precondition {
      condition     = var.gateway_certificate_arn != ""
      error_message = "gateway_certificate_arn must be a real ACM certificate ARN for the TLS listener on 443 (set it per environment in environments/*.tfvars); empty is only for validate-only runs."
    }
  }
}

resource "aws_ecs_service" "gateway" {
  name            = "relay-${var.environment}-gateway"
  cluster         = aws_ecs_cluster.gateway.id
  task_definition = aws_ecs_task_definition.gateway.arn
  desired_count   = var.gateway_node_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.gateway_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = "centrifugo"
    container_port   = 8000
  }

  # Overload sheds NEW connections first; existing ones drain on deploy.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 150

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ------------------------------------------------------------------------------
# Autoscaling. The gateway is connection- and memory-bound, NOT CPU-bound
# (RFC-001 §9), so we target-track on memory as the readily-available stand-in.
# Connection-count autoscaling is the true signal: Centrifugo exports a
# node_num_clients gauge; wire a custom CloudWatch metric + a
# CustomizedMetricSpecification target-tracking policy on it once the metric
# pipeline lands. CPU is deliberately NOT a scaling dimension here.
# ------------------------------------------------------------------------------

resource "aws_appautoscaling_target" "gateway" {
  max_capacity       = var.gateway_max_count
  min_capacity       = var.gateway_min_count
  resource_id        = "service/${aws_ecs_cluster.gateway.name}/${aws_ecs_service.gateway.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "gateway_memory" {
  name               = "relay-${var.environment}-gw-memory"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.gateway.resource_id
  scalable_dimension = aws_appautoscaling_target.gateway.scalable_dimension
  service_namespace  = aws_appautoscaling_target.gateway.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = 65
    scale_in_cooldown  = 300 # slow scale-in: connections drain gracefully
    scale_out_cooldown = 60  # fast scale-out: absorb reconnect storms
  }
}
