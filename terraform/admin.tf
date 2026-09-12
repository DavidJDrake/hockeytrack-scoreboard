# The admin site's API: who owns which panel, and what it is showing.
#
# Authorization is split deliberately. API Gateway's JWT authorizer proves the
# caller is a signed-in user of this pool and nothing more; the Lambda decides
# what that user may touch, by reading an ownership row. The alternative --
# federating Cognito identities into IoT policies with policy variables -- is
# a legitimate AWS pattern, but it pushes authorization into a policy document
# that cannot be unit-tested. This way the rule is Go, and Task 3 tests it.

resource "aws_dynamodb_table" "devices" {
  name         = "scoreboard-devices"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thingName"

  attribute {
    name = "thingName"
    type = "S"
  }
  attribute {
    name = "owner"
    type = "S"
  }
  attribute {
    name = "code"
    type = "S"
  }

  # "list my devices" is one query rather than a scan.
  global_secondary_index {
    name            = "owner-index"
    hash_key        = "owner"
    projection_type = "ALL"
  }

  # Resolving a pairing code is one query. Rows keep their code after
  # claiming; ByCode ignores a row that already has an owner, so a used code
  # stops resolving.
  global_secondary_index {
    name            = "code-index"
    hash_key        = "code"
    projection_type = "ALL"
  }
}

resource "aws_cognito_user_pool" "admin" {
  name = "scoreboard-admins"

  # Invite-only: nobody can create their own account. Users are created by the
  # administrator. An open sign-up form on a hobby project is an invitation to
  # abuse it, and this project hands panels to family, not to the public.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]

  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }
}

resource "aws_cognito_user_pool_domain" "admin" {
  domain       = "scoreboard-admin-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.admin.id
}

# A public client with no secret: the site is static, so a secret would be
# readable by anyone who views source. Authorization code with PKCE is the
# flow that does not need one.
resource "aws_cognito_user_pool_client" "site" {
  name         = "scoreboard-site"
  user_pool_id = aws_cognito_user_pool.admin.id

  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]

  # SRP proves a password without sending it, and refresh keeps a session
  # alive between the two; nothing here needs USER_PASSWORD_AUTH, which
  # would let a caller submit a password straight to Cognito for guessing.
  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  # Without this, AWS defaults new clients to LEGACY, which makes sign-in
  # error messages tell an unauthenticated caller whether a given email has
  # an account -- the same disclosure the API itself avoids by returning 404
  # rather than 403 (see admin-api.md).
  prevent_user_existence_errors = "ENABLED"

  # localhost:8000 is deliberate, not a leftover: it's the redirect_uri the
  # docs' curl walkthrough (docs/admin-api.md) uses to complete the PKCE
  # exchange from a human's browser without a hosted site to redirect to.
  callback_urls = ["https://${var.admin_site_origin}/", "http://localhost:8000/"]
  logout_urls   = ["https://${var.admin_site_origin}/"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

data "archive_file" "api" {
  type        = "zip"
  source_file = "${path.module}/../build/api/bootstrap"
  output_path = "${path.module}/../build/api.zip"
}

# See the comment on the reducer/today log groups in lambda.tf: the name here
# is a literal and depends_on inverts the create order for the same reason --
# without it, a first apply could let the function run (and auto-create AWS's
# own un-retained default log group) before this one exists, and this
# resource would then fail to create because the name is already taken.
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/scoreboard-api"
  retention_in_days = 30
}

resource "aws_iam_role" "api" {
  name               = "scoreboard-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "api" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
  # No PutItem: the only PutItem this package ever makes is Register, which
  # nothing in handler.go calls (registration is still by hand). PutItem
  # would also let a caller rewrite a row wholesale, voiding the
  # attribute_not_exists(owner) condition that makes Claim one-time -- so it
  # stays out even once Register gets a caller, in favor of a narrower grant.
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.devices.arn]
  }
  # Every Query in dynamo.go (ByCode, ListByOwner) names an index, never the
  # table itself.
  statement {
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.devices.arn}/index/*"]
  }
  # The one identity in this account allowed to publish a device's config, and
  # only to that topic shape. It cannot touch hockeytrack/games/*, which the
  # reducer owns.
  statement {
    actions   = ["iot:Publish", "iot:RetainPublish"]
    resources = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/scoreboard/*/config"]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  function_name    = "scoreboard-api"
  role             = aws_iam_role.api.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 10
  memory_size      = 128
  environment {
    variables = {
      DEVICES_TABLE = aws_dynamodb_table.devices.name
      IOT_ENDPOINT  = "https://${data.aws_iot_endpoint.data.endpoint_address}"
      SCHEDULE_URL  = var.schedule_url
    }
  }
  depends_on = [aws_cloudwatch_log_group.api]
}

resource "aws_apigatewayv2_api" "admin" {
  name          = "scoreboard-admin"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["https://${var.admin_site_origin}"]
    allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.admin.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.site.id]
    issuer   = "https://${aws_cognito_user_pool.admin.endpoint}"
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.admin.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

locals {
  admin_routes = [
    "GET /api/devices",
    "POST /api/devices/claim",
    "PUT /api/devices/{thing}/game",
    "PATCH /api/devices/{thing}",
    "DELETE /api/devices/{thing}",
    "GET /api/games",
  ]
}

resource "aws_apigatewayv2_route" "admin" {
  for_each           = toset(local.admin_routes)
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.admin.id
  name        = "$default"
  auto_deploy = true

  # Without stage-level limits, this API inherits the account-wide API
  # Gateway quota -- shared with two unrelated projects in this account -- so
  # one looping caller here would degrade both of them too. 20 rps / 40 burst
  # is generous for a handful of owners retargeting panels by hand and still
  # far below the account default.
  default_route_settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp",
      route     = "$context.routeKey", status = "$context.status",
      sub       = "$context.authorizer.claims.sub", error = "$context.error.message",
    })
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/scoreboard-admin"
  retention_in_days = 30
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.admin.execution_arn}/*/*"
}
