# Device enrollment: how a freshly flashed panel earns a certificate.
# Spec: docs/superpowers/specs/2026-09-12-device-enrollment-design.md

# One table, two kinds of item. An enrollment is keyed by the hash of its
# collection token; a code reservation is keyed by "code#" plus the hash of the
# code and points back at the enrollment. The reservation is what makes codes
# unique -- DynamoDB cannot enforce uniqueness on an index, but it can refuse a
# conditional write -- and it removes the need for an index at all.
#
# Deliberately NOT the scoreboard-devices table. That one is the irreplaceable
# ownership record (SCO-23); anonymous internet traffic has no business
# churning writes into it.
resource "aws_dynamodb_table" "enrollments" {
  name         = "scoreboard-enrollments"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Expiry is the database's job. Enrollments live a day, code reservations
  # fifteen minutes, and both carry expiresAt.
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_iam_role" "enroll" {
  name = "scoreboard-enroll"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "enroll" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.enroll.arn}:*"]
  }

  statement {
    sid = "Enrollments"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]
    resources = [aws_dynamodb_table.enrollments.arn]
  }

  # The ownership row, written once at claim. No DeleteItem: this function has
  # no reason to remove a device somebody owns.
  statement {
    sid       = "OwnershipRow"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.devices.arn]
  }

  # Minting a device identity. CreateCertificateFromCsr cannot be scoped to a
  # resource that does not exist yet, so the containment comes from the next
  # statement and from this role being reachable by nothing but this function.
  statement {
    sid       = "MintCertificate"
    actions   = ["iot:CreateCertificateFromCsr"]
    resources = ["*"]
  }

  statement {
    sid = "RegisterThing"
    actions = [
      "iot:CreateThing",
      "iot:DeleteThing",
      "iot:AttachThingPrincipal",
      "iot:DetachThingPrincipal",
      "iot:UpdateCertificate",
      "iot:DeleteCertificate",
    ]
    resources = [
      "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:thing/scoreboard-*",
      "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:cert/*",
    ]
  }

  # THE constraint. Unpinned, a bug in device onboarding becomes fleet-wide
  # compromise: mint a certificate, attach an over-permissive policy, and the
  # result is a credential nobody intended to exist. Pinned to one policy, the
  # worst an enrollment bug produces is another ordinary scoreboard.
  statement {
    sid       = "AttachOnlyTheDevicePolicy"
    actions   = ["iot:AttachPolicy", "iot:DetachPolicy"]
    resources = [aws_iot_policy.device.arn]
  }
}

resource "aws_iam_role_policy" "enroll" {
  name   = "scoreboard-enroll"
  role   = aws_iam_role.enroll.id
  policy = data.aws_iam_policy_document.enroll.json
}

resource "aws_cloudwatch_log_group" "enroll" {
  name              = "/aws/lambda/scoreboard-enroll"
  retention_in_days = 30
}

data "archive_file" "enroll" {
  type        = "zip"
  source_file = "${path.module}/../build/enroll/bootstrap"
  output_path = "${path.module}/../build/enroll.zip"
}

resource "aws_lambda_function" "enroll" {
  function_name    = "scoreboard-enroll"
  role             = aws_iam_role.enroll.arn
  filename         = data.archive_file.enroll.output_path
  source_code_hash = data.archive_file.enroll.output_base64sha256
  handler          = "bootstrap"
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  timeout          = 15

  environment {
    variables = {
      ENROLLMENTS_TABLE = aws_dynamodb_table.enrollments.name
      DEVICES_TABLE     = aws_dynamodb_table.devices.name
      IOT_ENDPOINT      = data.aws_iot_endpoint.data.endpoint_address
      DEVICE_POLICY     = aws_iot_policy.device.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.enroll]
}

resource "aws_apigatewayv2_integration" "enroll" {
  api_id                 = aws_apigatewayv2_api.admin.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.enroll.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_lambda_permission" "enroll_api" {
  statement_id  = "AllowAPIGatewayInvokeEnroll"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.enroll.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.admin.execution_arn}/*/*"
}

# Two of these are deliberately unauthenticated -- the only such routes in the
# project. A caller who reaches them can create a pending row and nothing else:
# no thing, no certificate, no policy attachment. Everything that grants access
# waits for POST /api/devices/claim, which the JWT authorizer guards.
resource "aws_apigatewayv2_route" "enroll_submit" {
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = "POST /api/enroll"
  target             = "integrations/${aws_apigatewayv2_integration.enroll.id}"
  authorization_type = "NONE"
}

# The device's own bearer token is checked in the function, not by an
# authorizer: it is not a JWT, and a token that does not resolve is answered
# 404 exactly like one that never existed.
resource "aws_apigatewayv2_route" "enroll_collect" {
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = "GET /api/enroll"
  target             = "integrations/${aws_apigatewayv2_integration.enroll.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_route" "enroll_claim" {
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = "POST /api/devices/claim"
  target             = "integrations/${aws_apigatewayv2_integration.enroll.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Enrollment is a handful of requests a week in normal use: somebody sets up a
# panel. A sustained rate means either an abuser filling the table with junk
# rows or a fleet of panels stuck in a retry loop, and both are worth knowing
# about the same day rather than at the end of the month.
resource "aws_cloudwatch_metric_alarm" "enroll_flood" {
  alarm_name          = "scoreboard-enroll-flood"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 100
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.enroll.function_name }
  alarm_description   = <<-EOT
    More than 100 enrollment calls in five minutes. Normal traffic is a few a
    week. Check the enrollment table for junk rows and the function's logs for
    whether the calls are submits (an abuser) or collects (panels retrying).
    The deliberate position in the spec is that this is where a WAF gets added
    if it is ever needed -- per-IP limiting was judged not worth a standing
    monthly cost to defend against an attack whose payoff is a few dollars of
    DynamoDB writes. This alarm firing is the evidence that changes that.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

# A claim that fails is somebody typing a code wrong, which happens. A lot of
# them is somebody guessing, and the codes are only eight characters.
#
# Scoped to this one route, not the whole API: an unscoped 4xx sum would also
# count a panel polling GET /api/enroll with a stale collection token (an
# expected, harmless 404) and every other admin route's ordinary client
# errors, which would make this alarm fire on noise and get muted. HTTP APIs
# only emit a route-level metric when detailed metrics are turned on for that
# route (see the POST /api/devices/claim route_settings block in admin.tf),
# and per AWS's own docs that per-route metric is dimensioned ApiId, Stage,
# Method, Resource -- HTTP APIs still call it Method/Resource, not Route,
# even though a route_key elsewhere in this file is written "POST /path".
resource "aws_cloudwatch_metric_alarm" "enroll_claim_failures" {
  alarm_name          = "scoreboard-enroll-claim-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 20
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/ApiGateway"
  metric_name         = "4xx"
  dimensions = {
    ApiId    = aws_apigatewayv2_api.admin.id
    Stage    = aws_apigatewayv2_stage.default.name
    Method   = "POST"
    Resource = "/api/devices/claim"
  }
  alarm_description  = <<-EOT
    More than 20 4xx responses from POST /api/devices/claim in five minutes.
    The likely innocent cause is somebody mistyping their pairing code; the
    one worth acting on is somebody working through the eight-character code
    space. Check the access log group for the source address.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "notBreaching"
}
