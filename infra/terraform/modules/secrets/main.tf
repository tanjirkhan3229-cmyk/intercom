# ------------------------------------------------------------------------------
# Secrets Manager entries referenced by ECS task definitions. Terraform only
# creates the containers (and an empty placeholder version); the real material
# is rotated in out-of-band so plaintext never lands in state or code
# (RFC-001 §13).
# ------------------------------------------------------------------------------

locals {
  secrets = {
    jwt_signing_key   = "relay/${var.environment}/jwt-signing-key"
    centrifugo_token  = "relay/${var.environment}/centrifugo-token-secret"
    centrifugo_apikey = "relay/${var.environment}/centrifugo-api-key"
    ses_credentials   = "relay/${var.environment}/ses-credentials"
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each                = local.secrets
  name                    = each.value
  description             = "Relay ${each.key} (${var.environment})."
  recovery_window_in_days = 7

  tags = {
    Name = each.value
  }
}

# Placeholder versions so task defs can reference a stable ARN pre-rotation.
# ignore_changes keeps Terraform from clobbering the real value once rotated in.
resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each      = aws_secretsmanager_secret.this
  secret_id     = each.value.id
  secret_string = "PLACEHOLDER_ROTATE_ME"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
