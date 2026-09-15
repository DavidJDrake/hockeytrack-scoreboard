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

  # "list my devices" is one query rather than a scan.
  global_secondary_index {
    name            = "owner-index"
    hash_key        = "owner"
    projection_type = "ALL"
  }
}

resource "aws_cognito_user_pool" "admin" {
  name = "scoreboard-admins"

  # Invite-only -- but this setting only closes the SignUp operation. It does
  # not stop a Google account's first sign-in from creating a profile; the
  # authgate function in lambda_config below does that, by admitting only
  # invited addresses (signin.tf). This stays true anyway, so the password
  # sign-up path is shut twice rather than once.
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

  # The load-bearing half of the owner-hint defense. The enroll flow decides
  # who owns a pre-bound panel by comparing the caller's email claim against a
  # hash typed into the panel's setup file (ownerMatches,
  # cloud/cmd/enroll/handler.go), so an invited user who could point their own
  # account at the owner's address could claim the owner's panel. With this
  # set, Cognito does not write the new address at all until it is verified:
  # the attempt sends a code to the *owner's* mailbox, the impostor's email
  # attribute keeps its old value, and their token therefore never carries the
  # owner's address in any state. The handler's own email_verified check stays
  # regardless -- it is what keeps a reverted or misconfigured pool safe -- but
  # this is the control that does not depend on how API Gateway serializes a
  # boolean claim.
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }

  # Both triggers are the same function, which tells them apart by the
  # event's triggerSource. V1_0 is the token generation event every Cognito
  # feature plan offers; the function changes no claims, so it needs nothing
  # newer.
  lambda_config {
    pre_sign_up = aws_lambda_function.authgate.arn
    pre_token_generation_config {
      lambda_arn     = aws_lambda_function.authgate.arn
      lambda_version = "V1_0"
    }
  }
}

resource "aws_cognito_user_pool_domain" "admin" {
  domain       = "scoreboard-admin-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.admin.id
}

# A public client with no secret: the site is static, so a secret would be
# readable by anyone who views source. Authorization code with PKCE is the
# flow that does not need one. Google is its only identity provider, so there
# is no password here to phish, guess or reset.
resource "aws_cognito_user_pool_client" "site" {
  name         = "scoreboard-site"
  user_pool_id = aws_cognito_user_pool.admin.id

  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  # Never add aws.cognito.signin.user.admin here; see write_attributes below.
  allowed_oauth_scopes         = ["openid", "email"]
  supported_identity_providers = [aws_cognito_identity_provider.google.provider_name]

  # No password flow of any kind, SRP included: with Google as the only way
  # in, the one thing a caller may do with this client directly is exchange a
  # refresh token. The list cannot simply be left out, because Cognito's
  # default for a client with none is to allow SRP and custom auth.
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  # Cognito only records an attribute mapped from an identity provider if the
  # app client can write it; otherwise it silently drops the value and signs
  # the user in anyway (AWS's documented behavior for IdP attribute mapping).
  # email is mapped from Google (signin.tf), and both the authgate gate and
  # enroll's ownerMatches depend on it being present, so it must be listed
  # here. email_verified is mapped too, but cannot be: Cognito rejects it in
  # WriteAttributes ("Invalid write attributes specified"), because it is not
  # one of the standard attributes a client may be granted. Whether Google's
  # verified flag still reaches the gate without it is observed at the first
  # real sign-in; if it does not, the gate refuses, which fails closed. The
  # list must never be empty -- write_attributes
  # is Optional+Computed, so an empty list is indistinguishable from omitting
  # the argument entirely, and the provider then leaves Cognito's default
  # writable set, which is every standard attribute.
  #
  # This does not reopen the door it once took two attributes to hold shut:
  # writing an attribute yourself means calling UpdateUserAttributes with an
  # access token carrying the aws.cognito.signin.user.admin scope, and
  # allowed_oauth_scopes above grants only openid and email, so no token this
  # client issues can make that call. That scope must never be added. Behind
  # it, the pool still refuses to write a new email before it is verified
  # (user_attribute_update_settings), Cognito updates the mapped email from
  # Google at sign-in whenever Google's value differs (AWS documents that for
  # IdP attribute mapping, but not how it combines with that update setting,
  # which nothing here has observed), and ownerMatches still requires
  # email_verified, which no client can write at all.
  #
  # If a real profile-editing feature is ever added, give it its own client --
  # not this one.
  write_attributes = ["email"]

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
  type             = "zip"
  source_file      = "${path.module}/../build/api/bootstrap"
  output_path      = "${path.module}/../build/api.zip"
  output_file_mode = "0755"
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

  # Enrollment is unauthenticated, so it shares the API's account-wide
  # exposure without even the JWT authorizer's cost to slow down a caller.
  # A tighter limit here keeps a flood of enrollment traffic from starving
  # the rest of the site, which the shared default_route_settings budget
  # would not do on its own.
  route_settings {
    route_key              = "POST /api/enroll"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }

  route_settings {
    route_key              = "GET /api/enroll"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }

  # Detailed metrics are off account-wide (a stage-level default, checked
  # before adding this), so without this the enroll_claim_failures alarm
  # (enroll.tf) would watch a route-level metric nothing ever emits and look
  # healthy while seeing no data at all. Turning it on for just this one
  # route is a deliberate, accepted cost -- per-route CloudWatch metrics are
  # billed as custom metrics -- against an alarm that would otherwise be
  # trustworthy for nothing.
  #
  # The throttle values repeat the stage default (20 rps / 40 burst) rather
  # than leaving them out to "inherit" it. A route_settings block with no
  # throttle fields is not guaranteed to mean "use default_route_settings" --
  # there is no version-controlled acceptance test for what API Gateway does
  # with an omitted RouteSettings throttle, and the risk if it means an
  # explicit 0 is throttling the one route that mints certificates and grants
  # ownership to nothing. Stating the intent costs two lines; an untested
  # assumption about inheritance does not belong on this route. Do not remove
  # these as "redundant" with default_route_settings.
  route_settings {
    route_key                = "POST /api/devices/claim"
    detailed_metrics_enabled = true
    throttling_rate_limit    = 20
    throttling_burst_limit   = 40
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
