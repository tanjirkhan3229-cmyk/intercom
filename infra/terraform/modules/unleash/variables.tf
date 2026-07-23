variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "region" {
  type        = string
  description = "AWS region (log configuration)."
}

variable "vpc_id" {
  type        = string
  description = "VPC id (for the target group)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Subnets for the Unleash task and its Postgres."
}

variable "alb_sg_id" {
  type        = string
  description = "Security group for the internal Unleash ALB (ingress 80 from the app tier)."
}

variable "task_sg_id" {
  type        = string
  description = "Security group for the Unleash task (ingress 4242 from the Unleash ALB)."
}

variable "db_sg_id" {
  type        = string
  description = "Security group for the Unleash backing Postgres (ingress 5432 from the Unleash task)."
}

variable "image" {
  type        = string
  description = "Unleash server container image."
  default     = "unleashorg/unleash-server:latest"
}

variable "cpu" {
  type        = number
  description = "Fargate CPU units for Unleash."
  default     = 256
}

variable "memory" {
  type        = number
  description = "Fargate memory (MiB) for Unleash."
  default     = 512
}

variable "db_instance_class" {
  type        = string
  description = "Instance class for the Unleash backing Postgres."
  default     = "db.t4g.micro"
}

variable "ecs_cluster_arn" {
  type        = string
  description = "ECS cluster to run the Unleash service in."
}
