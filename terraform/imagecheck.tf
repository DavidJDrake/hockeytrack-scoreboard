# The twice-daily divergence check between the image mirror and its GitHub
# Release (cmd/imagecheck). A mismatch is published to the security topic by
# the function itself; the alarms below are for the check failing to run at
# all, because a monitor that fails silently reads as "all clear". It runs
# twice a day, not once, purely so the not-running alarm below (built on
# Invocations' 24h maximum period) always has slack -- see that alarm's
# comment.

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
  # Lambda's CPU and network throughput scale with memory, not just RAM
  # headroom. The function streams the whole mirrored image through SHA-256
  # in one pass (io.Copy straight into the hasher), so both the S3 download
  # and the hashing need real CPU: at 256 MB (roughly an eighth of a vCPU)
  # a multi-hundred-MB to low-GB .img.xz could plausibly miss the 300s
  # timeout on a slow day. 1024 MB (roughly half a vCPU) gives both steps
  # enough throughput to comfortably clear a several-GB image well inside
  # 300s, so the timeout itself is left unchanged rather than papering over
  # a throughput problem with more wall-clock time.
  #
  # Since the update path (OTA design 6.5) it also hashes both update
  # payloads, about 0.7 GB more, and verifies the signed manifest against
  # the KMS key; the timeout grows to fit two more objects. In-region reads
  # still cost nothing.
  timeout     = 600
  memory_size = 1024
  environment {
    variables = {
      IMAGES_BUCKET  = aws_s3_bucket.images.bucket
      TOPIC_ARN      = data.aws_sns_topic.security_alerts.arn
      GITHUB_REPO    = "DavidJDrake/hockeytrack-scoreboard"
      SIGNING_KEY_ID = aws_kms_alias.release_signing.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.imagecheck]
}

resource "aws_scheduler_schedule" "imagecheck" {
  name       = "scoreboard-imagecheck"
  group_name = aws_scheduler_schedule_group.main.name
  # Twice a day (11:00 and 23:00 UTC), not once. The not-running alarm below
  # uses Invocations' longest period, 24h; on a once-daily schedule the
  # trailing 24h window can hold no datapoint just after the scheduled run
  # (yesterday's aged out of the window, today's not yet emitted -- Lambda's
  # own metric reporting lags a run), and "missing" is deliberately treated
  # as breaching, so that gap would false-page the security topic every day.
  # A second run 12 hours later means the window is never without at least
  # one real invocation.
  schedule_expression = "cron(0 11,23 * * ? *)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.imagecheck.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# EventBridge Scheduler invokes Lambda asynchronously, and Lambda's default
# async retry policy retries a failed invocation two more times (three
# attempts total) -- which means a disagreement already alerted on by the
# first attempt could be re-alerted by the second and third. The first
# attempt's own failure still trips scoreboard-imagecheck-errors, and the
# next scheduled run is only 12 hours away, so nothing is gained by retrying
# here and the risk of a tripled alert is removed by turning it off.
resource "aws_lambda_function_event_invoke_config" "imagecheck" {
  function_name          = aws_lambda_function.imagecheck.function_name
  maximum_retry_attempts = 0
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
    A check that the image mirror matches its GitHub Release could not
    finish. Until it runs, a replaced image would go unnoticed. Read
    /aws/lambda/scoreboard-imagecheck: a GitHub outage or rate limit clears on
    the next run; an S3 access error means the function's role or the bucket
    policy has changed, and should be compared with the scoreboard repository.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "notBreaching"
}

# The errors and throttles alarms above only fire when the function runs and
# something goes wrong; they say nothing if the schedule itself stops
# invoking it at all (a deleted or disabled schedule, a scheduler role that
# can no longer assume or invoke, or the function itself being deleted or
# renamed). 86400s is CloudWatch's maximum alarm period for this metric, and
# the schedule above deliberately runs twice within it (11:00 and 23:00 UTC)
# rather than once, so the trailing 24h window always contains at least one
# real invocation and this never false-pages purely from Lambda's own
# metric-reporting lag right after a scheduled run. treat_missing_data is
# deliberately "breaching" here (unlike the two alarms above) because
# Invocations publishes no data point at all when the function never runs --
# a true "went quiet", not a reported zero -- and a monitor that can go
# silently quiet is worse than one that pages on deploy day.
resource "aws_cloudwatch_metric_alarm" "imagecheck_not_running" {
  alarm_name          = "scoreboard-imagecheck-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 86400
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions = {
    FunctionName = aws_lambda_function.imagecheck.function_name
  }
  alarm_description  = <<-EOT
    The image mirror check has not run at all in the last 24 hours -- no
    Invocations data point, not merely zero successful runs, which should
    never happen since it is scheduled twice a day. Check that
    aws_scheduler_schedule.imagecheck still exists and is ENABLED, that
    aws_iam_role.scheduler (scheduler.tf) still trusts the
    schedule-group/scoreboard group ARN and is still allowed to invoke
    scoreboard-imagecheck, and that the function itself has not been
    deleted or renamed.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "breaching"
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
    A scheduled image mirror check was throttled and did not run. The
    account's Lambda concurrency is exhausted, or someone set a reserved
    concurrency on scoreboard-imagecheck; either way that run was skipped.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "notBreaching"
}
