output "url" {
  value       = "http://${aws_lb.unleash.dns_name}"
  description = "Internal Unleash base URL."
}

output "db_endpoint" {
  value       = aws_db_instance.unleash.address
  description = "Unleash backing Postgres hostname."
}
