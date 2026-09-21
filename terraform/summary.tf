# scoreboard-summary: once a minute, one retained document with every current
# game's score, for the panel's information strip (cloud/cmd/summary).
#
# Its own function and its own role rather than a second job for
# scoreboard-today, so that what it may do is written down in one place and
# is as little as the job needs: read the games table, write ONE topic. It
# takes no input and makes no outbound request, so unlike scoreboard-today it
# needs no route to the internet's content at all.

data "archive_file" "summary" {
  type             = "zip"
  source_file      = "${path.module}/../build/summary/bootstrap"
  output_path      = "${path.module}/../build/summary.zip"
  output_file_mode = "0755"
}

# A literal name, created before the function: see the comment on the
# reducer/today log groups in lambda.tf.
resource "aws_cloudwatch_log_group" "summary" {
  name              = "/aws/lambda/scoreboard-summary"
  retention_in_days = 30
}

resource "aws_iam_role" "summary" {
  name               = "scoreboard-summary"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "summary" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.summary.arn}:*"]
  }
  statement {
    # Scan and nothing else: it never writes a game, so a fault here cannot
    # put a wrong score on a panel's main screen, only leave a line out of
    # the strip.
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.games.arn]
  }
  statement {
    # One topic, by name. Not games/*: that is where the state documents
    # live, and this function has no business being able to write one.
    actions   = ["iot:Publish", "iot:RetainPublish"]
    resources = ["${local.topic_arn_prefix}/summary"]
  }
}

resource "aws_iam_role_policy" "summary" {
  role   = aws_iam_role.summary.id
  policy = data.aws_iam_policy_document.summary.json
}

resource "aws_lambda_function" "summary" {
  function_name    = "scoreboard-summary"
  role             = aws_iam_role.summary.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.summary.output_path
  source_code_hash = data.archive_file.summary.output_base64sha256
  timeout          = 15
  memory_size      = 128
  # One at a time. The function remembers what it last published so that it
  # can stay quiet when nothing changed, and two copies would each remember
  # something different. It also bounds what a runaway schedule could cost.
  reserved_concurrent_executions = 1
  environment {
    variables = {
      GAMES_TABLE  = aws_dynamodb_table.games.name
      IOT_ENDPOINT = "https://${data.aws_iot_endpoint.data.endpoint_address}"
    }
  }
  depends_on = [aws_cloudwatch_log_group.summary]
}

# A run that fails is followed by another within the minute; retrying the
# failed one as well would only stack them behind the concurrency limit.
resource "aws_lambda_function_event_invoke_config" "summary" {
  function_name          = aws_lambda_function.summary.function_name
  maximum_retry_attempts = 0
}

resource "aws_scheduler_schedule" "summary" {
  name                = "scoreboard-summary"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "rate(1 minute)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.summary.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# One failed minute is nothing: the next one replaces it. Ten in an hour is a
# function that has stopped working -- a role or a table changed under it --
# and the strip on every panel is going stale without saying so.
resource "aws_cloudwatch_metric_alarm" "summary_errors" {
  alarm_name          = "scoreboard-summary-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 10
  period              = 3600
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.summary.function_name
  }
  alarm_description  = <<-EOT
    The scores summary could not be published ten times in an hour. Panels
    keep showing their own game; the other-games line in the strip is going
    stale. Read /aws/lambda/scoreboard-summary. An AccessDenied means the
    function's role, the games table or the topic name no longer match
    terraform/summary.tf.
  EOT
  alarm_actions      = [data.aws_sns_topic.alerts.arn]
  treat_missing_data = "notBreaching"
}
