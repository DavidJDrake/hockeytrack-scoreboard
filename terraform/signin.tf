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
  description = "Addresses the invite list starts with. Read ONCE, when /scoreboard/allowed-emails is first created; after that the list is edited with aws ssm put-parameter and this variable changes nothing. Set it in the gitignored terraform.tfvars, never in this public repository."

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
  description = "Addresses allowed to sign in to ${var.site_domain}, comma-separated. Edit with aws ssm put-parameter --overwrite; Terraform never changes the value."
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
