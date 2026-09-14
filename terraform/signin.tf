# Sign in with Google, invite-only.
# Spec: docs/superpowers/specs/2026-09-13-google-sign-in-design.md
#
# The pool's allow_admin_create_user_only closes SignUp and nothing else: a
# Google account's first sign-in creates a profile regardless. So the invite
# list is enforced here, by one function the pool calls at both ends of an
# account's life -- when it is about to be created, and whenever tokens are
# issued -- which is what lets taking someone off the list end access they
# already have.

variable "invited_emails" {
  type        = list(string)
  sensitive   = true
  nullable    = false
  description = "Addresses the invite list starts with. Read ONCE, when /scoreboard/allowed-emails is first created; after that the list is edited with aws ssm put-parameter, and this variable matters again only if the parameter is replaced, which reseeds it. Set it in the gitignored terraform.tfvars, never in this public repository."

  validation {
    condition = length(var.invited_emails) > 0 && alltrue([
      for e in var.invited_emails : can(regex("^[^@,[:space:]]+@[^@,[:space:]]+$", e))
    ])
    error_message = "invited_emails needs at least one address, with no commas or spaces in any of them."
  }
}

# The invite list, as one comma-separated String -- the shape the ebook
# library's gate already runs on. Reading it takes ssm:GetParameter on this
# parameter, which the authgate role below holds and nothing else in this
# stack does.
resource "aws_ssm_parameter" "allowed_emails" {
  name        = "/scoreboard/allowed-emails"
  description = "Addresses allowed to sign in to ${var.site_domain}, comma-separated. Edit with aws ssm put-parameter --overwrite; Terraform does not change the value in place."
  type        = "String"
  value       = join(",", var.invited_emails)

  # Terraform seeds this once and then leaves the value alone: no apply
  # changes it in place, so none can quietly undo an invitation or reinstate
  # someone who was removed. A replacement is the exception -- it would reseed
  # the list from invited_emails -- and it shows as -/+ in a plan, which the
  # deploy task treats as stop-and-investigate.
  lifecycle {
    ignore_changes = [value]
  }
}

# The Google OAuth client's ID and secret, created by hand so the secret is
# typed into nothing that gets committed. It does still land in Terraform
# state, because the identity provider below stores it; the state bucket is
# encrypted and private, and the spec (section 4.3) names that trade. Never
# inspect this with `terraform show -json` or a plan's JSON form, which print
# sensitive values in the clear.
data "aws_secretsmanager_secret_version" "google_oauth" {
  secret_id = "scoreboard/google-oauth-client"
}

locals {
  google_oauth = jsondecode(data.aws_secretsmanager_secret_version.google_oauth.secret_string)
}

resource "aws_cognito_identity_provider" "google" {
  user_pool_id  = aws_cognito_user_pool.admin.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id        = local.google_oauth.client_id
    client_secret    = local.google_oauth.client_secret
    authorize_scopes = "openid email"

    # Cognito fills these in for a Google provider whether or not they are
    # sent, so leaving them out gives a plan that always wants to remove them.
    # They are Google's public endpoints, copied from what Cognito stored.
    attributes_url                = "https://people.googleapis.com/v1/people/me?personFields="
    attributes_url_add_attributes = "true"
    authorize_url                 = "https://accounts.google.com/o/oauth2/v2/auth"
    oidc_issuer                   = "https://accounts.google.com"
    token_request_method          = "POST"
    token_url                     = "https://www.googleapis.com/oauth2/v4/token"
  }

  # email_verified is mapped so the gate can refuse an address Google itself
  # has not verified. username is Google's stable account ID, never the
  # address, which a person can change.
  attribute_mapping = {
    email          = "email"
    email_verified = "email_verified"
    username       = "sub"
  }
}

resource "aws_cloudwatch_log_group" "authgate" {
  name              = "/aws/lambda/scoreboard-authgate"
  retention_in_days = 30
}

resource "aws_iam_role" "authgate" {
  name               = "scoreboard-authgate"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# Its own logs and one parameter. No Cognito permissions at all: the function
# only answers yes or no, and Cognito acts on the answer.
data "aws_iam_policy_document" "authgate" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.authgate.arn}:*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.allowed_emails.arn]
  }
}

resource "aws_iam_role_policy" "authgate" {
  name   = "scoreboard-authgate"
  role   = aws_iam_role.authgate.id
  policy = data.aws_iam_policy_document.authgate.json
}

data "archive_file" "authgate" {
  type        = "zip"
  source_file = "${path.module}/../build/authgate/bootstrap"
  output_path = "${path.module}/../build/authgate.zip"
}

# Cognito gives a trigger five seconds and then fails the sign-in, so a longer
# timeout would buy nothing.
resource "aws_lambda_function" "authgate" {
  function_name    = "scoreboard-authgate"
  role             = aws_iam_role.authgate.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.authgate.output_path
  source_code_hash = data.archive_file.authgate.output_base64sha256
  timeout          = 5
  memory_size      = 128
  environment {
    variables = {
      ALLOWLIST_PARAMETER = aws_ssm_parameter.allowed_emails.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.authgate]
}

# One grant covers both triggers: the same function, called by the same pool.
resource "aws_lambda_permission" "authgate_cognito" {
  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authgate.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.admin.arn
}

# One refused sign-in is somebody's uninvited Google account, or a relative
# signed in to the wrong profile. Three in an hour is somebody trying -- or
# the invite list being unreadable, which refuses everyone, the owner
# included, and logs the same line with a different reason.
resource "aws_cloudwatch_log_metric_filter" "signin_refused" {
  name           = "scoreboard-signin-refused"
  log_group_name = aws_cloudwatch_log_group.authgate.name
  pattern        = "\"sign-in refused\""

  metric_transformation {
    name      = "SignInRefused"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "signin_refused" {
  alarm_name          = "scoreboard-signin-refused"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 3
  period              = 3600
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "SignInRefused"
  alarm_description   = <<-EOT
    Three or more refused sign-ins to the admin site within an hour. Read
    /aws/lambda/scoreboard-authgate: each refusal logs its trigger, a reason
    and the email's domain, never the address. "not invited" from assorted
    domains is somebody probing. "invite list unavailable" means nobody at
    all can sign in until the SSM parameter is readable again.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

# The gate fails closed when it cannot run, which is the right failure and a
# silent one. A crash, a timeout or a throttle refuses the sign-in without
# writing "sign-in refused", so scoreboard-signin-refused never counts it, and
# the owner simply cannot get in.
#
# Lambda's Errors metric is not the answer, because every refusal is an Errors
# datapoint: the gate refuses by returning an error. Metric math subtracting
# refusals was rejected too, because a refusal's error and its log line can fall
# in adjacent five-minute periods and page on nothing. This filter matches only
# lines the Lambda runtime writes when the function itself fails. A refusal
# writes none of them: its REPORT line carries no Status field, which was
# checked against the live log group, and a test holds the pattern to that.
#
#   Runtime.ExitError, Runtime exited   the process died, including at startup
#   Status: error, Status: timeout      the platform's verdict on the invocation
#   Task timed out                      the older wording for a timeout
#   panic:                              a Go panic's own first line
#
# One failure can write several of these lines, so the metric counts lines, not
# failures. The threshold is one.
resource "aws_cloudwatch_log_metric_filter" "authgate_failures" {
  name           = "scoreboard-authgate-failures"
  log_group_name = aws_cloudwatch_log_group.authgate.name
  pattern        = "?\"Runtime.ExitError\" ?\"Runtime exited\" ?\"Status: error\" ?\"Status: timeout\" ?\"Task timed out\" ?\"panic:\""

  metric_transformation {
    name      = "AuthgateFailures"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "authgate_failures" {
  alarm_name          = "scoreboard-authgate-failures"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "AuthgateFailures"
  alarm_description   = <<-EOT
    The scoreboard sign-in gate (scoreboard-authgate) crashed or timed out. It
    fails closed, so nobody, the owner included, can sign in to the admin site
    while this lasts. Read /aws/lambda/scoreboard-authgate for the failing
    lines, and check the function's configuration against the scoreboard
    repository: an emptied ALLOWLIST_PARAMETER makes it exit at startup.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

# A throttled invocation never starts, so it writes no log line at all, and
# Lambda counts it in neither Invocations nor Errors. Only this metric sees it.
# The function has no reserved concurrency, so a throttle means the account's
# concurrency is exhausted, or someone set this function's to zero.
resource "aws_cloudwatch_metric_alarm" "authgate_throttles" {
  alarm_name          = "scoreboard-authgate-throttles"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.authgate.function_name }
  alarm_description   = <<-EOT
    The scoreboard sign-in gate (scoreboard-authgate) was throttled. It fails
    closed, so sign-in to the admin site is refused while this lasts. Check
    `aws lambda get-function-concurrency --function-name scoreboard-authgate`
    (it should have none) and the account's concurrent executions.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}
