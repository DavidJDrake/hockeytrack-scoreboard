# scoreboard-director: once a minute, and when a schedule is saved, works out
# which of each panel's kept games is current and sends it (cloud/cmd/director,
# design section 6). SCO-42.
#
# This is the second principal in the account that may publish a panel's
# config document; until it, exactly one could (scoreboard-api, admin.tf).
# Its own function and its own role, so that what it may do is written down
# here and is as little as the job needs:
#   - read the devices, accounts and games tables;
#   - ONE write: the game it sent, the stamp and its own marker, on a panel's
#     row (devices.MarkSent), and IAM below lists those attributes by name;
#   - publish, retained, to scoreboard/*/config and no other topic.
# It composes the document with the API's function (internal/panelconfig), so
# the wire format still has one author. It publishes only when a panel's game
# changes, a retry converges, and MaxPublishes (internal/director) caps one
# run, with the alarm at the bottom of this file on reaching the cap.
#
# The ticket names a templates table among its reads. There is no such table
# yet (templates are SCO-40, and the API refuses one on a schedule today), so
# there is no grant for it here; add one when the table exists, read-only.

data "archive_file" "director" {
  type             = "zip"
  source_file      = "${path.module}/../build/director/bootstrap"
  output_path      = "${path.module}/../build/director.zip"
  output_file_mode = "0755"
}

# A literal name, created before the function: see the comment on the
# reducer/today log groups in lambda.tf.
resource "aws_cloudwatch_log_group" "director" {
  name              = "/aws/lambda/scoreboard-director"
  retention_in_days = 30
}

resource "aws_iam_role" "director" {
  name               = "scoreboard-director"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "director" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.director.arn}:*"]
  }
  # Every claimed panel with a schedule is its work list, and there is no
  # index on "has a schedule": a Scan, filtered to claimed rows, of a table
  # the size of the fleet. GetItem is the one-panel run the API asks for
  # after a save. The only role with Scan on this table.
  statement {
    actions   = ["dynamodb:Scan", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.devices.arn]
  }
  # The one write. dynamodb:Attributes lists every attribute an UpdateItem
  # from this role may name -- the key, the owner it is conditioned on, and
  # the three it sets -- so a request that touched the schedule, the settings
  # or the owner would be refused by IAM before it reached the table, whatever
  # the code did. AWS requires dynamodb:ReturnValues alongside it; MarkSent
  # asks for none. No PutItem, no DeleteItem, no Query.
  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.devices.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:Attributes"
      values   = ["thingName", "owner", "gameId", "chosenAt", "sent"]
    }
    condition {
      test     = "StringEqualsIfExists"
      variable = "dynamodb:ReturnValues"
      values   = ["NONE", "UPDATED_OLD", "UPDATED_NEW"]
    }
  }
  # An owner's default settings, read only: the document carries the
  # settings, laid together exactly as the API lays them, and one composed
  # without the account's layer would wipe those settings from the panel.
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.accounts.arn]
  }
  # One game's state at a time, read only: whether a kept game is live or
  # final, and when it ended. The reducer remains the only writer.
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.games.arn]
  }
  # The same topic shape as the API's grant and nothing else: not
  # hockeytrack/games/*, which the reducer owns, and not summary.
  statement {
    actions   = ["iot:Publish", "iot:RetainPublish"]
    resources = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/scoreboard/*/config"]
  }
}

resource "aws_iam_role_policy" "director" {
  role   = aws_iam_role.director.id
  policy = data.aws_iam_policy_document.director.json
}

resource "aws_lambda_function" "director" {
  function_name    = "scoreboard-director"
  role             = aws_iam_role.director.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.director.output_path
  source_code_hash = data.archive_file.director.output_base64sha256
  # The schedule fetch is given six seconds; the tables and a handful of
  # publishes take well under the rest.
  timeout     = 15
  memory_size = 128
  # One at a time. The minute sweep and a save-triggered run for one panel
  # would each decide the same thing from the same rows -- a collision is a
  # duplicate publish, never a disagreement -- but one copy bounds what a
  # runaway schedule could cost, and the save's invoke simply queues behind
  # the sweep for a few seconds.
  reserved_concurrent_executions = 1
  environment {
    variables = {
      DEVICES_TABLE  = aws_dynamodb_table.devices.name
      ACCOUNTS_TABLE = aws_dynamodb_table.accounts.name
      GAMES_TABLE    = aws_dynamodb_table.games.name
      IOT_ENDPOINT   = "https://${data.aws_iot_endpoint.data.endpoint_address}"
      SCHEDULE_URL   = var.schedule_url
    }
  }
  depends_on = [aws_cloudwatch_log_group.director]
}

# A run that fails is followed by another within the minute; retrying the
# failed one as well would only stack them behind the concurrency limit.
resource "aws_lambda_function_event_invoke_config" "director" {
  function_name          = aws_lambda_function.director.function_name
  maximum_retry_attempts = 0
}

resource "aws_scheduler_schedule" "director" {
  name                = "scoreboard-director"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "rate(1 minute)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.director.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# The API asks for a run for one panel after its schedule is saved (cmd/api,
# Handler.Direct). That grant is the API role's lambda:InvokeFunction on this
# one function, in admin.tf; a same-account IAM role needs no
# aws_lambda_permission on this side. Nobody else may invoke it but the
# scheduler (scheduler.tf).

# One failed minute is nothing: the next one replaces it. Ten in an hour is a
# function that has stopped working, and every scheduled panel is stuck on
# whatever game it was last sent without saying so.
resource "aws_cloudwatch_metric_alarm" "director_errors" {
  alarm_name          = "scoreboard-director-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 10
  period              = 3600
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.director.function_name
  }
  alarm_description  = <<-EOT
    The director failed ten times in an hour. Panels with a schedule keep
    showing the game they were last sent and will not move to the next one.
    Read /aws/lambda/scoreboard-director. An AccessDenied means the
    function's role, a table or the topic name no longer match
    terraform/director.tf; a refused UpdateItem in particular means the
    dynamodb:Attributes condition there and devices.MarkSent disagree about
    which attributes the write names. A "season:" error means HockeyTrack's
    schedule file could not be read.
  EOT
  alarm_actions      = [data.aws_sns_topic.alerts.arn]
  treat_missing_data = "notBreaching"
}

# The ceiling. A run that wants to change more panels than MaxPublishes
# (internal/director) logs this line once and leaves the rest for the next
# minute. The fleet is a handful of panels, so any datapoint here is a
# schedule source or a clock that has gone wrong and was about to move every
# panel at once, not a busy night. The pattern is director.CeilingMessage,
# quoted, and assumes Lambda's default text log format, as the token
# mismatch filters in admin.tf do.
resource "aws_cloudwatch_log_metric_filter" "director_ceiling" {
  name           = "scoreboard-director-ceiling"
  log_group_name = aws_cloudwatch_log_group.director.name
  pattern        = "\"publish ceiling reached\""

  metric_transformation {
    name      = "DirectorCeiling"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "director_ceiling" {
  alarm_name          = "scoreboard-director-ceiling"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "DirectorCeiling"
  alarm_description   = <<-EOT
    A director run hit its publish ceiling: it wanted to change the game on
    more panels in one minute than the fleet has. It sent the first
    MaxPublishes and stopped. Something upstream is wrong, not the panels:
    compare HockeyTrack's schedule file against the NHL's for moved or
    missing games, check the games table for states that flipped, and check
    the function's clock against the start times it logged. The line
    "publish ceiling reached" in /aws/lambda/scoreboard-director marks the
    run.
  EOT
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}
