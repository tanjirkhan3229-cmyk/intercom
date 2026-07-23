# ------------------------------------------------------------------------------
# VPC with public + private subnets across every AZ, IGW, one NAT per AZ, and
# the security-group mesh for the alb / app / gateway / rds / redis tiers.
# ------------------------------------------------------------------------------

locals {
  # Deterministic /20 carve-out per AZ: public subnets low, private subnets high.
  public_subnet_cidrs  = [for i, _ in var.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnet_cidrs = [for i, _ in var.azs : cidrsubnet(var.vpc_cidr, 4, i + 8)]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "relay-${var.environment}"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "relay-${var.environment}-igw"
  }
}

resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "relay-${var.environment}-public-${var.azs[count.index]}"
    Tier = "public"
  }
}

resource "aws_subnet" "private" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = var.azs[count.index]

  tags = {
    Name = "relay-${var.environment}-private-${var.azs[count.index]}"
    Tier = "private"
  }
}

# ---- NAT: one EIP + NAT gateway per AZ, placed in the public subnet ----

resource "aws_eip" "nat" {
  count  = length(var.azs)
  domain = "vpc"

  tags = {
    Name = "relay-${var.environment}-nat-${var.azs[count.index]}"
  }
}

resource "aws_nat_gateway" "this" {
  count         = length(var.azs)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "relay-${var.environment}-nat-${var.azs[count.index]}"
  }

  depends_on = [aws_internet_gateway.this]
}

# ---- Route tables ----

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name = "relay-${var.environment}-public"
  }
}

resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = length(var.azs)
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }

  tags = {
    Name = "relay-${var.environment}-private-${var.azs[count.index]}"
  }
}

resource "aws_route_table_association" "private" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ------------------------------------------------------------------------------
# Security groups. Rules are wired so each tier only accepts from the tier in
# front of it: internet -> ALB/NLB -> app/gateway -> rds/redis.
# ------------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "relay-${var.environment}-alb"
  description = "Public ALB for the app tier."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP (redirect to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-alb"
  }
}

resource "aws_security_group" "gateway" {
  name        = "relay-${var.environment}-gateway"
  description = "Centrifugo gateway tasks (websockets behind the NLB)."
  vpc_id      = aws_vpc.this.id

  # NLB target groups preserve the client IP, so the websocket port is opened to
  # the VPC CIDR (NLB has no security group of its own).
  ingress {
    description = "Centrifugo websocket/API port from within the VPC"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-gateway"
  }
}

resource "aws_security_group" "app" {
  name        = "relay-${var.environment}-app"
  description = "App / worker / beat Fargate tasks."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "App port from the ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-app"
  }
}

resource "aws_security_group" "rds" {
  name        = "relay-${var.environment}-rds"
  description = "RDS Postgres; only reachable from app and gateway tiers."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Postgres from the app tier"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  ingress {
    description     = "Postgres from the gateway tier (Unleash-adjacent workloads)"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.gateway.id]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-rds"
  }
}

resource "aws_security_group" "redis" {
  name        = "relay-${var.environment}-redis"
  description = "ElastiCache Redis (cache/pubsub + broker)."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Redis from the app tier"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  ingress {
    description     = "Redis from the gateway tier (Centrifugo engine)"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.gateway.id]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-redis"
  }
}

# ------------------------------------------------------------------------------
# Unleash tier security groups. The feature-flag server, its internal ALB, and
# its own backing Postgres each get a dedicated SG so intra-tier traffic is
# explicit (AWS does NOT implicitly allow intra-SG traffic) and the Unleash DB
# is isolated from the shared app/worker/beat/gateway tasks.
#
# Chain: app -> unleash-alb (80) -> unleash-task (4242) -> unleash-db (5432).
# ------------------------------------------------------------------------------

resource "aws_security_group" "unleash_alb" {
  name        = "relay-${var.environment}-unleash-alb"
  description = "Internal ALB fronting the Unleash UI/API."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "HTTP from the app tier"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-unleash-alb"
  }
}

resource "aws_security_group" "unleash_task" {
  name        = "relay-${var.environment}-unleash-task"
  description = "Unleash feature-flag Fargate task."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Unleash HTTP from its internal ALB"
    from_port       = 4242
    to_port         = 4242
    protocol        = "tcp"
    security_groups = [aws_security_group.unleash_alb.id]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-unleash-task"
  }
}

resource "aws_security_group" "unleash_db" {
  name        = "relay-${var.environment}-unleash-db"
  description = "Dedicated Postgres for Unleash; only reachable from the Unleash task."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Postgres from the Unleash task"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.unleash_task.id]
  }

  egress {
    description = "All egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "relay-${var.environment}-unleash-db"
  }
}
