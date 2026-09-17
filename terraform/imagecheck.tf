# The daily divergence check between the image mirror and its GitHub Release
# (cmd/imagecheck). A mismatch is published to the security topic by the
# function itself; the alarms below are for the check failing to run at all,
# because a monitor that fails silently reads as "all clear".

data "archive_file" "imagecheck" {
  type             = "zip"
  source_file      = "${path.module}/../build/imagecheck/bootstrap"
  output_path      = "${path.module}/../build/imagecheck.zip"
  output_file_mode = "0755"
}

resource "aws_cloudwatch_log_group" "imagecheck" {
  name              = "/aws/lambda/scoreboard-imagecheck"
  retention_in_days = 30
}

resource "aws_iam_role" "imagecheck" {
  name               = "scoreboard-imagecheck"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# ListBucket is what makes a missing object a 404 rather than a 403, so "not
# published yet" can be told apart from "access broken".
data "aws_iam_policy_document" "imagecheck" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.imagecheck.arn}:*"]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.images.arn]
  }
  statement {
    actions   = ["sns:Publish"]
    resources = [data.aws_sns_topic.security_alerts.arn]
  }
}

resource "aws_iam_role_policy" "imagecheck" {
  role   = aws_iam_role.imagecheck.id
  policy = data.aws_iam_policy_document.imagecheck.json
}

resource "aws_lambda_function" "imagecheck" {
  function_name    = "scoreboard-imagecheck"
  role             = aws_iam_role.imagecheck.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.imagecheck.output_path
  source_code_hash = data.archive_file.imagecheck.output_base64sha256
  timeout          = 300
  memory_size      = 256
  environment {
    variables = {
      IMAGES_BUCKET = aws_s3_bucket.images.bucket
      TOPIC_ARN     = data.aws_sns_topic.security_alerts.arn
      GITHUB_REPO   = "DavidJDrake/hockeytrack-scoreboard"
    }
  }
  depends_on = [aws_cloudwatch_log_group.imagecheck]
}

resource "aws_scheduler_schedule" "imagecheck" {
  name                = "scoreboard-imagecheck"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "cron(0 11 * * ? *)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.imagecheck.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "imagecheck_errors" {
  alarm_name          = "scoreboard-imagecheck-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 3600
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.imagecheck.function_name
  }
  alarm_description  = <<-EOT
    The daily check that the image mirror matches its GitHub Release could not
    finish. Until it runs, a replaced image would go unnoticed. Read
    /aws/lambda/scoreboard-imagecheck: a GitHub outage or rate limit clears on
    the next run; an S3 access error means the function's role or the bucket
    policy has changed, and should be compared with the scoreboard repository.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "imagecheck_throttles" {
  alarm_name          = "scoreboard-imagecheck-throttles"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 3600
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions = {
    FunctionName = aws_lambda_function.imagecheck.function_name
  }
  alarm_description  = <<-EOT
    The daily image mirror check was throttled and did not run. The account's
    Lambda concurrency is exhausted, or someone set a reserved concurrency on
    scoreboard-imagecheck; either way the mirror went unchecked.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "notBreaching"
}
