# Partial backend config. Real bucket/key/region/dynamodb_table are supplied at
# `terraform init -backend-config=...` per environment so no state lives in code.
terraform {
  backend "s3" {}
}
