output "distribution_domain_name" {
  value       = aws_cloudfront_distribution.this.domain_name
  description = "CloudFront distribution domain name."
}

output "distribution_id" {
  value       = aws_cloudfront_distribution.this.id
  description = "CloudFront distribution id."
}

output "widget_bucket" {
  value       = aws_s3_bucket.widget.bucket
  description = "Widget bucket name."
}
