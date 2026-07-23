output "endpoint" {
  value       = aws_db_instance.this.address
  description = "RDS hostname."
}

output "port" {
  value       = aws_db_instance.this.port
  description = "RDS port."
}

output "master_user_secret_arn" {
  value       = try(aws_db_instance.this.master_user_secret[0].secret_arn, null)
  description = "Secrets Manager ARN of the RDS-managed master password."
}
