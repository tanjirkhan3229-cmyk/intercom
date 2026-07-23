# ------------------------------------------------------------------------------
# Self-hosted Unleash feature-flag server: a small Fargate service plus its own
# backing Postgres (isolated from the app database). Reachable only from the app
# tier and the internal ALB.
# ------------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "relay-${var.environment}-unleash-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The DB password lives only in the RDS-managed Secrets Manager secret. The task
# execution role pulls it at container start, so it needs GetSecretValue on that
# secret (and kms:Decrypt for the default AWS-managed key that encrypts it — an
# aws/secretsmanager alias grant via the service condition covers a CMK too).
data "aws_iam_policy_document" "exec_secrets_read" {
  statement {
    sid       = "ReadUnleashDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.unleash.master_user_secret[0].secret_arn]
  }

  statement {
    sid     = "DecryptUnleashDbSecret"
    actions = ["kms:Decrypt"]
    # RDS encrypts the managed secret with the AWS-managed aws/secretsmanager key
    # by default; scope the decrypt grant to the Secrets Manager service.
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "exec_secrets" {
  name   = "relay-${var.environment}-unleash-db-secret-read"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.exec_secrets_read.json
}

resource "aws_iam_role" "task" {
  name               = "relay-${var.environment}-unleash-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# ---- Backing Postgres (its own tiny instance) ----

resource "aws_db_subnet_group" "unleash" {
  name       = "relay-${var.environment}-unleash"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "unleash" {
  identifier     = "relay-${var.environment}-unleash"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "unleash"
  username = "unleash"
  # Password managed by RDS + Secrets Manager (no plaintext).
  manage_master_user_password = true

  multi_az               = false
  db_subnet_group_name   = aws_db_subnet_group.unleash.name
  vpc_security_group_ids = [var.db_sg_id]

  backup_retention_period = 7
  deletion_protection     = true
  skip_final_snapshot     = false

  final_snapshot_identifier = "relay-${var.environment}-unleash-final"

  tags = {
    Name = "relay-${var.environment}-unleash"
  }
}

# ---- Internal ALB for the Unleash UI/API ----

resource "aws_lb" "unleash" {
  name               = "relay-${var.environment}-unleash"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.private_subnet_ids

  tags = {
    Name = "relay-${var.environment}-unleash"
  }
}

resource "aws_lb_target_group" "unleash" {
  name        = "relay-${var.environment}-unleash"
  port        = 4242
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    interval            = 15
    matcher             = "200"
  }
}

resource "aws_lb_listener" "unleash" {
  load_balancer_arn = aws_lb.unleash.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.unleash.arn
  }
}

# ---- ECS service ----

resource "aws_cloudwatch_log_group" "unleash" {
  name              = "/relay/${var.environment}/unleash"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "unleash" {
  family                   = "relay-${var.environment}-unleash"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "unleash"
      image     = var.image
      essential = true
      portMappings = [
        { containerPort = 4242, protocol = "tcp" }
      ]
      environment = [
        { name = "DATABASE_HOST", value = aws_db_instance.unleash.address },
        { name = "DATABASE_PORT", value = "5432" },
        { name = "DATABASE_NAME", value = "unleash" },
        { name = "DATABASE_USERNAME", value = "unleash" },
        { name = "DATABASE_SSL", value = "true" },
      ]
      # DB password injected from the RDS-managed Secrets Manager secret (JSON),
      # selecting the "password" key. Unleash reads DATABASE_PASSWORD when the
      # DATABASE_* parts are supplied individually.
      secrets = [
        {
          name      = "DATABASE_PASSWORD"
          valueFrom = "${aws_db_instance.unleash.master_user_secret[0].secret_arn}:password::"
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.unleash.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "unleash"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "unleash" {
  name            = "relay-${var.environment}-unleash"
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.unleash.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.task_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.unleash.arn
    container_name   = "unleash"
    container_port   = 4242
  }
}
