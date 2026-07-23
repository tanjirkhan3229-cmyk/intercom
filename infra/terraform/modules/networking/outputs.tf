output "vpc_id" {
  value       = aws_vpc.this.id
  description = "VPC id."
}

output "public_subnet_ids" {
  value       = aws_subnet.public[*].id
  description = "Public subnet ids (ALB, NLB, NAT)."
}

output "private_subnet_ids" {
  value       = aws_subnet.private[*].id
  description = "Private subnet ids (Fargate tasks, RDS, Redis)."
}

output "alb_sg_id" {
  value       = aws_security_group.alb.id
  description = "ALB security-group id."
}

output "app_sg_id" {
  value       = aws_security_group.app.id
  description = "App/worker/beat security-group id."
}

output "gateway_sg_id" {
  value       = aws_security_group.gateway.id
  description = "Gateway (Centrifugo) security-group id."
}

output "rds_sg_id" {
  value       = aws_security_group.rds.id
  description = "RDS security-group id."
}

output "redis_sg_id" {
  value       = aws_security_group.redis.id
  description = "Redis security-group id."
}

output "unleash_alb_sg_id" {
  value       = aws_security_group.unleash_alb.id
  description = "Unleash internal ALB security-group id."
}

output "unleash_task_sg_id" {
  value       = aws_security_group.unleash_task.id
  description = "Unleash task security-group id."
}

output "unleash_db_sg_id" {
  value       = aws_security_group.unleash_db.id
  description = "Unleash backing Postgres security-group id."
}
