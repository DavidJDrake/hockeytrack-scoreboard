# The admin site: scoreboard.davidjdrake.com
#
# A private S3 bucket behind CloudFront with origin access control, an ACM
# certificate validated through Route 53, and alias records in the
# davidjdrake.com zone. Mirrors the pattern hockeytrack's site.tf already uses,
# because two sites in one account that differ arbitrarily are two sites to
# reason about instead of one.
#
# Nothing is built on it yet. It exists now so the name resolves, the
# certificate path is proven, and there is somewhere for the image download
# page to live when the image build lands. A holding page ships with it: a
# domain that resolves to a 404 is worse than one that resolves to "not yet".

variable "site_domain" {
  type        = string
  default     = "scoreboard.davidjdrake.com"
  description = "Host the admin site is served from. Keep in step with admin_site_origin, which drives CORS and the Cognito callbacks."
}

variable "site_zone_name" {
  type    = string
  default = "davidjdrake.com."
}

data "aws_route53_zone" "site" {
  name         = var.site_zone_name
  private_zone = false
}

resource "aws_s3_bucket" "site" {
  bucket = "scoreboard-site-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "scoreboard-site"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "site_bucket" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.site.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.site_bucket.json
}

# CloudFront requires the certificate in us-east-1, which is this provider's
# region.
resource "aws_acm_certificate" "site" {
  domain_name       = var.site_domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "site_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.site.domain_validation_options : dvo.domain_name => {
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

resource "aws_acm_certificate_validation" "site" {
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.site_cert_validation : r.fqdn]
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Security headers on every response.
#
# The CSP is deliberately wider than hockeytrack's, and only where it has to
# be: this site signs users in against Cognito and calls the admin API, so
# connect-src has to name both. Everything else stays self-only. Both hosts are
# interpolated from the resources themselves rather than typed in, so the
# policy cannot drift away from the API it is describing.
resource "aws_cloudfront_response_headers_policy" "site" {
  name = "scoreboard-site-security"

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
    content_security_policy {
      content_security_policy = join("", [
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; ",
        "img-src 'self' data:; font-src 'self'; ",
        "connect-src 'self' ${aws_apigatewayv2_api.admin.api_endpoint} https://cognito-idp.${var.region}.amazonaws.com; ",
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'",
      ])
      override = true
    }
  }
}

# Clean URLs: /panels/ and /panels both serve /panels/index.html.
resource "aws_cloudfront_function" "index_rewrite" {
  name    = "scoreboard-index-rewrite"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
      } else if (!uri.split('/').pop().includes('.')) {
        request.uri = uri + '/index.html';
      }
      return request;
    }
  EOT
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  default_root_object = "index.html"
  aliases             = [var.site_domain]
  price_class         = "PriceClass_100"
  comment             = var.site_domain

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "site-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id           = "site-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.site.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.index_rewrite.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "site_a" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = var.site_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "site_aaaa" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = var.site_domain
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

# A holding page, managed here rather than uploaded by hand so the name never
# resolves to a 404 and so this file is the whole story of the domain.
resource "aws_s3_object" "holding_page" {
  bucket        = aws_s3_bucket.site.id
  key           = "index.html"
  content_type  = "text/html; charset=utf-8"
  cache_control = "public, max-age=300"
  content       = <<-HTML
    <!doctype html>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>HockeyTrack Scoreboard</title>
    <style>
      :root { color-scheme: dark }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center;
             background: #0a0a0c; color: #fafafa;
             font: 16px/1.6 system-ui, sans-serif }
      main { max-width: 34rem; padding: 2rem }
      h1 { font-size: 1.6rem; margin: 0 0 1rem }
      p { color: #969eA8; margin: 0 0 1rem }
      a { color: #fafafa }
    </style>
    <main>
      <h1>HockeyTrack Scoreboard</h1>
      <p>A physical NHL scoreboard: a Raspberry Pi driving a bar display that
         follows one live game, pushed from HockeyTrack's event bus through
         AWS IoT Core within about a second of the play.</p>
      <p>This is where the device image and the panel admin will live. Neither
         is ready yet.</p>
      <p><a href="https://github.com/DavidJDrake/hockeytrack-scoreboard">The code, in the meantime</a>.</p>
    </main>
  HTML
}

output "site_url" {
  value = "https://${var.site_domain}"
}

output "site_bucket" {
  value = aws_s3_bucket.site.bucket
}

output "site_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}
