# scoreboard-sweep: once a day, name every panel that nobody owns and the
# broker has not seen for 365 days (cloud/cmd/sweep, internal/sweep). The
# backstop behind the Released list, SCO-33, step 5 of retiring a panel.
#
# DRY-RUN ONLY, AND NOT BY A SWITCH. The retire action is SCO-32's Lambda,
# which does not exist yet, and the ticket requires one full cycle of logged
# would-retire lines before the sweep may act at all. So this function reads
# and logs, and this role can do nothing else: no iot:DeleteCertificate, no
# iot:DeleteThing, no iot:UpdateCertificate, no DetachPolicy, no DeleteItem,
# no UpdateItem, no Publish. Act mode, when it comes, is a deliberate change
# here -- lambda:InvokeFunction on the retire function, and this role named
# in step 4's alarm allow list -- and a default-off switch in the code, and
# a reviewer can see from this file alone that neither exists today.
#
# THIS ROLE IS NAMED IN HOCKEYTRACK'S SCOREBOARD-STATE RULE. That rule
# (hockeytrack terraform/security-alarms.tf, section 14) pages the security
# topic on every DynamoDB data event on the devices table from any role but
# the ones on its allow list, and the sweep's daily Scan is such an event.
# The role is on that list by name, scoreboard-sweep, with the reason
# recorded there: one read-only Scan a day, and this policy as the proof.
# Renaming this role, or giving it a write, is a change to make there too;
# an unlisted name pages every day at 09:15 UTC, which is the loud failure
# and the intended one. Apply order: this stack creates the role before
# HockeyTrack's data source can read it, or that plan fails, also loudly.
#
# HOW A PANEL'S LAST CONNECTION IS KNOWN. Nothing recorded it before this
# ticket; the panel cannot publish (iot.tf), so its only trace of life is the
# broker's own connect/disconnect record. Two ways to get at that were
# weighed on the ticket's own test, the least standing permission:
#
#   Chosen: fleet indexing (iot.tf), read through iot:SearchIndex. The
#   permission added is a read, on one index, to this role and the API's.
#   No new principal can write anything, and the ownership table gains no
#   writer. The index is AWS's; when it says a panel connected, the broker
#   saw that panel's certificate, which is stronger than any stamp this
#   stack could write for itself.
#
#   Rejected: an IoT rule on $aws/events/presence/connected/+ writing a
#   lastSeen stamp onto the panel's devices row. The brief assumed the rule
#   could UpdateItem one attribute; it cannot. Both of the rule engine's
#   DynamoDB actions (dynamodb and dynamodbv2) are PutItem, which replaces
#   the whole row -- the first connect of a claimed panel would have erased
#   its owner. Doing it correctly means a rule, a Lambda and a role holding
#   UpdateItem on the ownership table (conditioned to one attribute, as the
#   director's is), plus lifecycle events turned on account-wide. That is a
#   standing write on the one table that cannot be rebuilt, granted to a
#   principal driven by broker events, to record a fact AWS already holds.
#   A read beat it.
#
#   Not covered by the chosen way, plainly: the index knows nothing before
#   the day it is turned on (iot.tf says how that shows), it is eventually
#   consistent by a few seconds, and iot:SearchIndex cannot be scoped below
#   the whole account's things (admin.tf, on the API's grant).

data "archive_file" "sweep" {
  type             = "zip"
  source_file      = "${path.module}/../build/sweep/bootstrap"
  output_path      = "${path.module}/../build/sweep.zip"
  output_file_mode = "0755"
}

# A literal name, created before the function: see the comment on the
# reducer/today log groups in lambda.tf. 400 days, not 30: the dry-run cycle
# the ticket asks for is a year, and the record of what the sweep would have
# done must outlive it.
resource "aws_cloudwatch_log_group" "sweep" {
  name              = "/aws/lambda/scoreboard-sweep"
  retention_in_days = 400
}

resource "aws_iam_role" "sweep" {
  name               = "scoreboard-sweep"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# Two reads. Everything the sweep may ever do in dry-run is here, and a
# statement that is not here does not exist.
data "aws_iam_policy_document" "sweep" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.sweep.arn}:*"]
  }
  # The unowned rows are its work list, and there is no index on "has no
  # owner" (owner-index is sparse the other way): a Scan, filtered to rows
  # without one, of a table the size of the fleet. Read-only: no GetItem
  # even, since the list is the whole read. This is the statement the
  # scoreboard-state rule's allow-list entry (header) rests on: a write
  # added here is a write that rule no longer sees.
  statement {
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.devices.arn]
  }
  # When each of those panels last connected, from the fleet index. The
  # index is one resource for the whole account; this is the narrowest
  # grant the action allows.
  statement {
    actions   = ["iot:SearchIndex"]
    resources = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:index/AWS_Things"]
  }
}

resource "aws_iam_role_policy" "sweep" {
  role   = aws_iam_role.sweep.id
  policy = data.aws_iam_policy_document.sweep.json
}

resource "aws_lambda_function" "sweep" {
  function_name    = "scoreboard-sweep"
  role             = aws_iam_role.sweep.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.sweep.output_path
  source_code_hash = data.archive_file.sweep.output_base64sha256
  timeout          = 30
  memory_size      = 128
  # One at a time, and one a day: two copies could only log the same panels
  # twice, but one copy bounds what a runaway schedule could cost.
  reserved_concurrent_executions = 1
  environment {
    variables = {
      DEVICES_TABLE = aws_dynamodb_table.devices.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.sweep]
}

# A run that fails is followed by another the next day, and a retry of a
# read-only run has nothing to make good.
resource "aws_lambda_function_event_invoke_config" "sweep" {
  function_name          = aws_lambda_function.sweep.function_name
  maximum_retry_attempts = 0
}

# Daily, in the small hours Eastern (UTC-4/-5), when no panel is being set
# up and the log line lands in one place per day.
resource "aws_scheduler_schedule" "sweep" {
  name                = "scoreboard-sweep"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "cron(15 9 * * ? *)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.sweep.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# The dry-run record. Every panel the rule selects is one line, and this
# counts them into a metric so the owner sees each selection as it happens
# rather than reading a year of logs at the end. The pattern is
# sweep.WouldRetireMessage, quoted, and assumes Lambda's default text log
# format, as the token mismatch filters in admin.tf do.
resource "aws_cloudwatch_log_metric_filter" "sweep_would_retire" {
  name           = "scoreboard-sweep-would-retire"
  log_group_name = aws_cloudwatch_log_group.sweep.name
  pattern        = "\"would retire\""

  metric_transformation {
    name      = "SweepWouldRetire"
    namespace = "Scoreboard"
    value     = "1"
  }
}

# The ops topic, not the security one: a selection is the sweep working as
# designed, and the owner is asked to check it against the panel in the
# drawer. A selection that is wrong -- a panel that is in fact in use --
# is what the dry-run cycle exists to find, and this page is how.
#
# Five-minute periods, not one a day, and this is what lets it page more
# than once. An alarm's actions run on a change of state, not on a
# datapoint; in dry-run nothing changes between runs, so the same panel is
# named every morning, and with a day-long period the alarm would enter
# ALARM on the first selection and sit there for the rest of the cycle,
# paging once in a year. With five-minute periods and missing data
# notBreaching, it is back in OK within ten minutes of a run, and the next
# morning's selection is a fresh transition and a fresh page. So the page is
# per run that names a panel, not per line (a run naming three is one page
# with Sum 3; the names are in the log), and a panel that stays named pages
# every day until it is retired by hand: docs/retiring-a-panel.md says so,
# and that pressure is the point. site/tests/sweep-config.test.js holds the
# period under a day for this alarm and the ceiling's.
resource "aws_cloudwatch_metric_alarm" "sweep_would_retire" {
  alarm_name          = "scoreboard-sweep-would-retire"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "SweepWouldRetire"
  alarm_description   = <<-EOT
    The daily sweep found a panel that nobody owns and the broker has not
    seen for a year. It did nothing: the sweep is dry-run and cannot act
    (terraform/sweep.tf). The panel's name and last connection are on the
    "would retire" line in /aws/lambda/scoreboard-sweep. Decide whether
    that panel is really gone; if it is in use, the rule or the index is
    wrong and act mode must not be turned on until that is understood. To
    retire it today, docs/retiring-a-panel.md, by hand.
  EOT
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}

# The ceiling, held in dry-run too. A run that finds more than MaxRetire
# (internal/sweep) panels counts the first MaxRetire as would-retire, logs
# this line once, and names the rest on "past the ceiling" lines that no
# filter counts: in dry-run nothing changes between runs, so without those
# lines the same five would be cut every day and the rest would never reach
# the record. The fleet is a handful of panels, so any datapoint here is a
# clock or a query that has gone wrong and would have emptied the fleet in
# act mode, not a busy year. The pattern is sweep.CeilingMessage, quoted.
# The alarm's period is five minutes for the reason given on the
# would-retire alarm: a ceiling that is still being hit tomorrow must page
# again tomorrow, and a day-long period would leave it in ALARM, silent.
resource "aws_cloudwatch_log_metric_filter" "sweep_ceiling" {
  name           = "scoreboard-sweep-ceiling"
  log_group_name = aws_cloudwatch_log_group.sweep.name
  pattern        = "\"retire ceiling reached\""

  metric_transformation {
    name      = "SweepCeiling"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "sweep_ceiling" {
  alarm_name          = "scoreboard-sweep-ceiling"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "SweepCeiling"
  alarm_description   = <<-EOT
    A sweep run wanted to name more panels than its ceiling in one day:
    more unowned, year-silent panels than the fleet plausibly has. It
    counted the first MaxRetire (internal/sweep) as "would retire", named
    the rest on "past the ceiling" lines, and in any case it is dry-run.
    Something upstream is wrong, not the panels: check the
    function's clock against the lastSeen values it logged, and check the
    fleet index (`aws iot search-index --index-name AWS_Things
    --query-string "thingName:scoreboard*"`) for connectivity records that
    are missing or stale. Do not turn on act mode with this alarm
    unexplained.
  EOT
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}

# One failed day is a missed day of the dry-run record. Three in a row is a
# function that has stopped working, and the year's record has a hole in it
# nobody would otherwise notice until the end. Day-long periods are right
# here, unlike the two alarms above: this pages once, on the third failed
# day, and stays in ALARM until a run succeeds, which is the shape a stuck
# function should have; a page a day for the same fault would add nothing.
resource "aws_cloudwatch_metric_alarm" "sweep_errors" {
  alarm_name          = "scoreboard-sweep-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 3
  threshold           = 1
  period              = 86400
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.sweep.function_name
  }
  alarm_description  = <<-EOT
    The daily sweep failed three days running, so the dry-run record has a
    gap. Read /aws/lambda/scoreboard-sweep. An AccessDenied means the
    function's role, the devices table or the index name no longer match
    terraform/sweep.tf; an index error means fleet indexing (iot.tf) is off
    or still building (`aws iot describe-index --index-name AWS_Things`).
  EOT
  alarm_actions      = [data.aws_sns_topic.alerts.arn]
  treat_missing_data = "notBreaching"
}
