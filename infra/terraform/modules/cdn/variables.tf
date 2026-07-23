variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "widget_bucket_name" {
  type        = string
  description = "S3 bucket name for widget bundles."
}

variable "domain_names" {
  type        = list(string)
  description = "Alternate domain names (CNAMEs) for the distribution. Empty uses the default cloudfront.net domain."
  default     = []
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM cert ARN (us-east-1). Empty uses the default CloudFront cert."
  default     = ""
}
