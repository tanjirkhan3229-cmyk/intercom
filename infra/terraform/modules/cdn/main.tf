# ------------------------------------------------------------------------------
# Widget bundle S3 origin fronted by one CloudFront distribution. The bucket
# blocks all public access; CloudFront reaches it via an Origin Access Control
# (OAC), and the bucket policy allows only that distribution.
#
# Attachments are NOT served through CloudFront: the app hands out presigned S3
# GET URLs (per-workspace prefix auth), so no attachments origin/behavior exists
# here — that keeps private tenant blobs off a world-readable CDN path.
# ------------------------------------------------------------------------------

locals {
  widget_origin_id = "widget-s3"
}

# ---- Widget bucket ----

resource "aws_s3_bucket" "widget" {
  bucket = var.widget_bucket_name

  tags = {
    Name = var.widget_bucket_name
  }
}

resource "aws_s3_bucket_public_access_block" "widget" {
  bucket                  = aws_s3_bucket.widget.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "widget" {
  bucket = aws_s3_bucket.widget.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "widget" {
  bucket = aws_s3_bucket.widget.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---- Origin Access Control (OAC) ----

resource "aws_cloudfront_origin_access_control" "this" {
  name                              = "relay-${var.environment}-oac"
  description                       = "OAC for Relay S3 origins."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ---- Distribution ----

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "Relay ${var.environment} (widget)."
  default_root_object = "loader.js"
  aliases             = var.domain_names

  origin {
    origin_id                = local.widget_origin_id
    domain_name              = aws_s3_bucket.widget.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  # Widget bundles: long-lived, immutable, aggressively cached.
  default_cache_behavior {
    target_origin_id       = local.widget_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.acm_certificate_arn == "" ? true : false
    acm_certificate_arn            = var.acm_certificate_arn == "" ? null : var.acm_certificate_arn
    ssl_support_method             = var.acm_certificate_arn == "" ? null : "sni-only"
    minimum_protocol_version       = var.acm_certificate_arn == "" ? "TLSv1" : "TLSv1.2_2021"
  }

  price_class = "PriceClass_100"

  tags = {
    Name = "relay-${var.environment}"
  }
}

# ---- Bucket policy: allow only this distribution via OAC ----

data "aws_iam_policy_document" "widget" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.widget.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "widget" {
  bucket = aws_s3_bucket.widget.id
  policy = data.aws_iam_policy_document.widget.json
}
