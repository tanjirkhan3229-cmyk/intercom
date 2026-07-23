output "jwt_signing_key_arn" {
  value       = aws_secretsmanager_secret.this["jwt_signing_key"].arn
  description = "ARN of the JWT signing key secret."
}

output "centrifugo_token_arn" {
  value       = aws_secretsmanager_secret.this["centrifugo_token"].arn
  description = "ARN of the Centrifugo token HMAC secret."
}

output "centrifugo_apikey_arn" {
  value       = aws_secretsmanager_secret.this["centrifugo_apikey"].arn
  description = "ARN of the Centrifugo API key secret."
}

output "ses_credentials_arn" {
  value       = aws_secretsmanager_secret.this["ses_credentials"].arn
  description = "ARN of the SES credentials secret."
}

output "all_arns" {
  value       = [for s in aws_secretsmanager_secret.this : s.arn]
  description = "All secret ARNs (for IAM read grants on the task exec role)."
}
