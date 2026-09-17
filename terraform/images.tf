# The image mirror: where strangers download the scoreboard SD card image.
# GitHub Releases is the source of truth; this is a copy, in a bucket and
# distribution separate from the website so that compromising the site cannot
# swap the image people flash (docs/superpowers/specs/2026-09-12-device-image-design.md,
# 6.4 and 9.5). HockeyTrack's security-alarms.tf section 15 pages on any write
# to it that is not the release itself, and cmd/imagecheck compares it twice a
# day against the release.

locals {
  images_domain = "images.${var.site_domain}"
}

resource "aws_s3_bucket" "images" {
  bucket = "scoreboard-images-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket                  = aws_s3_bucket.images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "images" {
  bucket = aws_s3_bucket.images.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# A replaced object keeps its previous version, so an overwritten image can be
# recovered and compared.
resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images" {
  bucket = aws_s3_bucket.images.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "images" {
  name                              = "scoreboard-images"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "images_bucket" {
  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.images.arn]
    }
  }
  statement {
    sid       = "TLSOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.images.arn, "${aws_s3_bucket.images.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "images" {
  bucket     = aws_s3_bucket.images.id
  policy     = data.aws_iam_policy_document.images_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.images]
}

resource "aws_acm_certificate" "images" {
  domain_name       = local.images_domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "images_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.images.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
  zone_id         = data.aws_route53_zone.site.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "images" {
  certificate_arn         = aws_acm_certificate.images.arn
  validation_record_fqdns = [for r in aws_route53_record.images_cert_validation : r.fqdn]
}

# latest.json changes with every release; a minute is how long a new release
# can take to appear. Everything under images/ is immutable per version.
resource "aws_cloudfront_cache_policy" "images_latest" {
  name        = "scoreboard-images-latest"
  min_ttl     = 0
  default_ttl = 60
  max_ttl     = 60
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# The download page on the site reads latest.json cross-origin; nothing else
# needs to.
resource "aws_cloudfront_response_headers_policy" "images" {
  name = "scoreboard-images"
  cors_config {
    access_control_allow_credentials = false
    access_control_allow_headers {
      items = ["Content-Type"]
    }
    access_control_allow_methods {
      items = ["GET", "HEAD"]
    }
    access_control_allow_origins {
      items = ["https://${var.site_domain}"]
    }
    access_control_max_age_sec = 600
    origin_override            = true
  }
  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
  }
}

resource "aws_cloudfront_distribution" "images" {
  enabled         = true
  is_ipv6_enabled = true
  http_version    = "http2and3"
  aliases         = [local.images_domain]
  price_class     = "PriceClass_100"
  comment         = local.images_domain

  origin {
    domain_name              = aws_s3_bucket.images.bucket_regional_domain_name
    origin_id                = "images-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.images.id
  }

  default_cache_behavior {
    target_origin_id           = "images-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.images.id
  }

  ordered_cache_behavior {
    path_pattern               = "/latest.json"
    target_origin_id           = "images-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.images_latest.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.images.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.images.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "images_a" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.images_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.images.domain_name
    zone_id                = aws_cloudfront_distribution.images.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "images_aaaa" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.images_domain
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.images.domain_name
    zone_id                = aws_cloudfront_distribution.images.hosted_zone_id
    evaluate_target_health = false
  }
}

# The GitHub OIDC provider belongs to the davidjdrake.com repository's
# Terraform (terraform/github_oidc.tf there). It is looked up, not created: a
# second definition would fight over the same provider. Deleting it there
# stops this repository's releases from publishing.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# GitHub gives a job the subject repo:<owner>/<repo>:environment:<name> only
# when it runs in that environment, and image-release is restricted to v*
# tags behind a required reviewer. So this role trusts an approved release,
# not a tag push, a branch, a pull request, or another repository.
data "aws_iam_policy_document" "image_publisher_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:DavidJDrake/hockeytrack-scoreboard:environment:image-release"]
    }
  }
}

resource "aws_iam_role" "image_publisher" {
  name                 = "scoreboard-image-publisher"
  assume_role_policy   = data.aws_iam_policy_document.image_publisher_trust.json
  max_session_duration = 3600
}

# Upload and invalidate, and nothing else besides reading and listing
# latest.json itself: the publish workflow reads latest.json before writing
# it so an out-of-order approval can never move it backwards, and needs
# s3:ListBucket scoped to that one key because a plain GetObject can't tell a
# missing key from one denied by policy without it (Task 3's "Mirror to the
# image CDN" step; controller ruling on Task 3's review, spec 9.5). No
# delete, no read or list of anything else, no other bucket.
data "aws_iam_policy_document" "image_publisher" {
  statement {
    sid       = "Upload"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.images.arn}/images/*", "${aws_s3_bucket.images.arn}/latest.json"]
  }
  statement {
    sid       = "ReadLatest"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images.arn}/latest.json"]
  }
  statement {
    sid       = "ListLatestOnly"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.images.arn]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = ["latest.json"]
    }
  }
  statement {
    sid       = "Invalidate"
    actions   = ["cloudfront:CreateInvalidation"]
    resources = [aws_cloudfront_distribution.images.arn]
  }
}

resource "aws_iam_role_policy" "image_publisher" {
  name   = "publish-images"
  role   = aws_iam_role.image_publisher.id
  policy = data.aws_iam_policy_document.image_publisher.json
}

output "images_bucket" {
  value = aws_s3_bucket.images.bucket
}

output "images_distribution_id" {
  value = aws_cloudfront_distribution.images.id
}

output "image_publisher_role_arn" {
  value = aws_iam_role.image_publisher.arn
}

output "images_domain" {
  value = local.images_domain
}
