provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "relay"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
