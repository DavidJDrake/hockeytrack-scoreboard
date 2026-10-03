# A permitted publish to a panel's config topic by anyone but the API or the
# director (SCO-72; the SCO-42 design, section 9).
#
# Exactly two identities in this account may publish to scoreboard/*/config:
# scoreboard-api (admin.tf) and scoreboard-director (director.tf). Every
# alarm in iot-alarms.tf fires on a publish that was DENIED. A third
# principal that was granted the permission -- a widened policy, a new role,
# a misapplied change, an administrator's own keys -- publishes without a
# denial, and until this file nothing paged: a successful data-plane publish
# is not an ERROR-level IoT log line (iot-logging.tf), and CloudTrail does not
# record IoT data-plane calls at all. This file makes the third publisher
# visible.
#
# How. An IoT topic rule on scoreboard/+/config writes one line per publish
# -- who, which topic, when; not the payload -- into a log group of its own.
# A metric filter counts every line whose principal is not one of the two
# roles, and an alarm pages the security topic on the first one. The API and
# the director publish on every game change, so the exclusion lives in the
# filter, where a legitimate publish never becomes a datapoint, and not in
# the alarm, where a threshold above the fleet's normal rate would hide a
# single intruder in a busy night.
#
# The alternative rejected: turning IoT logging up to INFO, which does log a
# line per successful publish, with the principal and the topic. Three
# reasons. IoT's V2 logging levels are set per account, thing group, client
# ID, source IP, principal ID or event type -- there is no per-topic-filter
# target -- so INFO would have to cover every publish on every topic, which
# the reducer's game events dominate, and the filter would then run over the
# shared AWSIotLogsV2 group instead of a group that holds only this. Any
# change to the logging level is an IoT control-plane write, which
# HockeyTrack's section 7 rule pages on, so the apply itself would look like
# tampering. And the pinned provider's aws_iot_logging_options has no
# per-target level anyway. The rule costs less and touches nothing shared.
#
# Cost, us-east-1 list prices: $0.15 per million rule triggers plus $0.15 per
# million actions; a line of about 150 bytes per publish into CloudWatch
# Logs; one custom metric ($0.30 a month) and two alarms ($0.10 each). A
# fleet of a handful of panels publishes a few dozen times a day, so the two
# alarms and the metric are the whole bill: under $0.50 a month.

# --- What principal() returns, and the pattern that reads it --------------
#
# The AWS IoT SQL reference's table for principal(): for an X.509 client, the
# certificate thumbprint; for an IAM user or role over HTTP or MQTT over
# WebSocket, "{{userid}}"; for the console's MQTT client,
# "{{iam-role-id}}:{{session-name}}". A role's userid, as STS reports it, is
# the role's unique ID and the session name joined by a colon, so the two
# Lambdas' lines are expected to read AROA...:scoreboard-api and
# AROA...:scoreboard-director. NOT the assumed-role ARN, and NOT the role's
# name, which is what a first draft of this file matched on and which would
# have counted every legitimate publish. The unique IDs are read from the
# role resources (aws_iam_role.api.unique_id, aws_iam_role.director.unique_id)
# rather than typed, so a role recreated with a new ID, or renamed -- which
# recreates it -- updates the filter in the same apply rather than silently
# exempting nothing.
#
# Which form the real lines carry is what the PENDING verification below
# settles; the table above is the documentation's, not an observation from
# this account. The failure mode if the documentation is wrong is loud, not
# silent: a principal in any other form matches neither exclusion, so the
# first API publish after the apply pages, and the pattern is corrected then.
# A line with no principal field at all, or one whose principal is JSON
# null, also counts, for the same reason: a blank is a shape nobody expected,
# not proof of a legitimate publisher. The two are separate terms because
# CloudWatch's JSON filter treats them differently: NOT EXISTS is false for a
# key that is present with a null value, and a string comparison against a
# null is false too, so without the IS NULL term a null principal matched
# nothing and was the one shape that was exempt by accident.
#
# The exact pattern, with A and D standing for the two unique IDs:
#   { ($.principal NOT EXISTS) || ($.principal IS NULL) || (($.principal != "A*") && ($.principal != "D*")) }
# Checked with `aws logs test-metric-filter` (read-only) on 2026-09-30 UTC,
# with twenty-character placeholder IDs in place of A and D so that a
# one-letter stand-in could not prefix-match the AIDA line by accident.
# Does not match, so never a datapoint:
#   {"principal":"A:scoreboard-api","topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000000}
#   {"principal":"D:scoreboard-director","topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000001}
#   {"principal":"A","topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000002}
# Matches, and pages:
#   {"principal":"AIDAEXAMPLEUSER0000","topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000003}
#   {"principal":"ba67293af50bf2506f5f93469686da660c7c844e7b3950bfb16813e0d31e9373","topic":"scoreboard/scoreboard-01/config","clientId":"scoreboard-02","publishedAt":1790000000004}
#   {"topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000005}
#   {"principal":null,"topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000006}
#   {"principal":"","topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000007}
#   {"principal":42,"topic":"scoreboard/scoreboard-01/config","clientId":"n/a","publishedAt":1790000000008}
# The third excluded line is the bare-userid form the reference gives for
# HTTP; the trailing wildcard covers both forms so the verification cannot
# turn on which one AWS meant. Without the IS NULL term the same nine lines
# gave five matches, the null line the one missing; with it, six.

# One line per publish. principal() and topic() are the detection; clientid()
# says whether it came over MQTT (a device certificate, which should be
# impossible under iot.tf) or over HTTPS ("n/a", so a SigV4 caller); the
# timestamp is the broker's. The payload is left out on purpose: it is the
# owner's game and display settings, none of which the alarm needs, and a
# log group is one more place they would otherwise be readable.
#
# The rule sees only publishes the broker accepted. A denied publish never
# reaches the rule engine, and stays the job of iot-alarms.tf.
resource "aws_iot_topic_rule" "config_publish" {
  name        = "scoreboard_config_publish"
  description = "One log line per accepted publish to a panel's config topic, so a publisher other than the API or the director can be alarmed on"
  enabled     = true
  sql         = "SELECT principal() AS principal, topic() AS topic, clientid() AS clientId, timestamp() AS publishedAt FROM 'scoreboard/+/config'"
  sql_version = "2016-03-23"

  cloudwatch_logs {
    log_group_name = aws_cloudwatch_log_group.config_publish.name
    role_arn       = aws_iam_role.config_publish_log.arn
  }
}

# 30 days, like the Lambda groups that hold the other half of any
# investigation (the API's log names the caller's sub and route). One short
# line per publish is all it ever holds, the alarm fires within five minutes
# of the line, and nothing else reads this group. HockeyTrack's section 13
# rule watches /aws/lambda/scoreboard-* and the API access log for retention
# and metric-filter changes by name; this group is not on that list yet, so
# a shortened retention or a deleted filter here pages nobody until
# HockeyTrack adds it. That is SCO-73, filed so the blind spot has an owner
# rather than a footnote.
resource "aws_cloudwatch_log_group" "config_publish" {
  name              = "/aws/iot/scoreboard-config-publish"
  retention_in_days = 30
}

# The role IoT assumes to write those lines. The confused-deputy conditions
# follow iot-logging.tf; unlike V2 logging, a topic rule has one resource
# ARN to pin aws:SourceArn to, and the AWS rule-action docs give it as
# arn:aws:iot:<region>:<account>:rule/<name>. Built from the name rather than
# read from the resource, because the rule references this role and Terraform
# would otherwise see a cycle. If the condition is wrong, IoT cannot assume
# the role, the action fails on every publish, and the Failure alarm at the
# bottom of this file pages: a wrong trust policy is loud here.
data "aws_iam_policy_document" "config_publish_log_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:rule/scoreboard_config_publish"]
    }
  }
}

resource "aws_iam_role" "config_publish_log" {
  name               = "scoreboard-config-publish-log"
  assume_role_policy = data.aws_iam_policy_document.config_publish_log_trust.json
}

# The three actions the CloudWatch Logs rule action documents as required,
# on this one group and nothing else. No CreateLogGroup (the group exists
# above), no PutRetentionPolicy (iot-logging.tf says why), and no iot:Publish
# or iot:RetainPublish of any kind: a republish action would make this role
# a third publisher, which is the very thing this file exists to alarm on.
data "aws_iam_policy_document" "config_publish_log" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.config_publish.arn}:*"]
  }
}

resource "aws_iam_role_policy" "config_publish_log" {
  role   = aws_iam_role.config_publish_log.id
  policy = data.aws_iam_policy_document.config_publish_log.json
}

# The pattern above, with the two unique IDs filled in by Terraform.
resource "aws_cloudwatch_log_metric_filter" "config_publish_by_other" {
  name           = "scoreboard-config-publish-by-other"
  log_group_name = aws_cloudwatch_log_group.config_publish.name
  pattern        = "{ ($.principal NOT EXISTS) || ($.principal IS NULL) || (($.principal != \"${aws_iam_role.api.unique_id}*\") && ($.principal != \"${aws_iam_role.director.unique_id}*\")) }"

  metric_transformation {
    name      = "ConfigPublishByOther"
    namespace = "Scoreboard"
    value     = "1"
  }
}

# No ok_actions, as for the security alarms in iot-alarms.tf: the first page
# has started the investigation, and "it stopped" says nothing new.
resource "aws_cloudwatch_metric_alarm" "config_publish_by_other" {
  alarm_name          = "scoreboard-config-publish-by-other"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "ConfigPublishByOther"
  alarm_description   = <<-EOT
    Something other than scoreboard-api or scoreboard-director published to
    a panel's config topic, and the broker let it. The line in
    /aws/iot/scoreboard-config-publish names the principal (an AROA/AIDA
    user ID for an IAM caller, a certificate thumbprint for a device), the
    topic, and whether it came over MQTT (clientId) or HTTPS (n/a). Only
    those two roles hold a publish grant to scoreboard/*/config, so this is
    either a principal that was given one -- find the change in HockeyTrack's
    IAM and IoT rules around the time -- or an administrator's own keys, in
    which case treat the credential as compromised. Then read the retained
    message on that topic to see what the panel was told. If it fires on the
    first API publish after an apply and names AROA...:scoreboard-api, the
    principal() form is not the one config-publish-alarm.tf expects; fix
    the filter, do not raise the threshold.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

# The rule going blind. Two ways, and this alarm sums both. If the action
# cannot write -- the trust condition above is wrong, the role loses a
# permission, the group is gone -- the publish still happens and no line is
# written, which the alarm above cannot see (missing data is notBreaching,
# on purpose); IoT emits Failure per rule action for that. And if the rules
# engine never reaches the action, Failure is not emitted at all: a payload
# it cannot parse is counted as ParseError on the rule instead. The SQL reads
# no payload field, and the reference documents SELECT * as safe on a
# non-JSON payload, but says nothing about a SELECT of functions alone -- so
# a non-JSON config, which still blanks a panel when it is retained, might
# produce no line, no Failure, and without this term no page. Each term goes
# to the security topic, not the ops topic, because a detection that has
# stopped detecting is the security event; HockeyTrack's trail-silent alarm
# makes the same call.
#
# Failure's dimensions are RuleName and ActionType; that query names the rule
# and lets ActionType be whatever AWS calls the CloudWatch Logs action,
# which nothing here has observed -- the same reason iot-alarms.tf does not
# pin Protocol. ParseError's only dimension is RuleName, so it is a plain
# metric, not a second query: a GetMetricData call, which is what an alarm
# evaluates through, allows exactly one Metrics Insights query (the Metrics
# Insights quotas page), and the account refused a two-query form on
# 2026-09-30 UTC with "Maximum number of queries (1) exceeded". The
# three-part form below -- the query, the metric, the sum -- parsed against
# this account with `aws cloudwatch get-metric-data` the same day. Neither
# metric exists until the rule has run once. Not summed: RuleExecutionThrottled,
# which the reference reserves for abuse of basic ingest at rates a fleet of
# a handful of panels does not approach.
resource "aws_cloudwatch_metric_alarm" "config_publish_log_failure" {
  alarm_name        = "scoreboard-config-publish-log-failure"
  alarm_description = <<-EOT
    The scoreboard_config_publish IoT rule did not write its log line for a
    publish it saw, so the config-publish-by-other alarm is blind for as long
    as this lasts. Either the action failed (Failure): check that the
    scoreboard-config-publish-log role's trust policy names the rule's ARN,
    that its policy still covers /aws/iot/scoreboard-config-publish, and that
    the group exists. Or the rules engine could not parse the payload
    (ParseError): something published a non-JSON config, which the API and
    the director never do, so treat it as the by-other alarm firing and read
    the retained message on each panel's config topic. Until it is fixed, a
    publish by a third principal leaves no record anywhere.
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]

  metric_query {
    id          = "action_failures"
    expression  = "SELECT SUM(Failure) FROM SCHEMA(\"AWS/IoT\", ActionType, RuleName) WHERE RuleName = 'scoreboard_config_publish'"
    period      = 60
    return_data = false
  }

  metric_query {
    id          = "parse_errors"
    return_data = false

    metric {
      namespace   = "AWS/IoT"
      metric_name = "ParseError"
      period      = 60
      stat        = "Sum"
      dimensions = {
        RuleName = "scoreboard_config_publish"
      }
    }
  }

  # A period with neither metric is missing data, not zero, so the sum is
  # missing too and the alarm stays notBreaching; a period with one of them
  # is that one's count. That second half is not obvious -- metric math
  # could just as well have dropped every period the absent metric lacked,
  # which would be every period, and this alarm would never have paged --
  # so it was checked read-only on 2026-09-30 UTC with `aws cloudwatch
  # get-metric-data`: a metric this account has (PublishIn.Success) plus
  # ParseError for this rule, which does not exist yet, summed to the first
  # metric's values in every period, for a plain metric and for the Metrics
  # Insights query alike. What that did not check is a metric that exists
  # but has a gap in one period; the absent-metric case is the one that
  # holds until the rule first fails, and it is the one that matters.
  metric_query {
    id          = "config_publish_log_failures"
    expression  = "action_failures + parse_errors"
    label       = "Rule action failures plus parse errors"
    return_data = true
  }
}

# --- Verification: PENDING ------------------------------------------------
#
# The proof is one deliberate publish from the owner's own credentials to a
# test thing's config topic (`aws iot-data publish --topic
# scoreboard/<test-thing>/config --payload ...` from an administrator's keys,
# us-east-1), which only the owner may do. Expected: one line in
# /aws/iot/scoreboard-config-publish naming an AIDA/AROA principal that is
# neither role, one datapoint on Scoreboard/ConfigPublishByOther, the
# config-publish-by-other alarm in ALARM within five minutes, and no page
# from the log-failure alarm. Then let the API or the director publish once
# and confirm the line's principal reads <unique id>:<function name> and
# produces no datapoint. Then one more publish with a payload that is not
# JSON (`--payload 'not json'`, to the same test thing): expected is EITHER
# a line from the by-other filter OR one ParseError datapoint and a page
# from the log-failure alarm; which one settles whether a SELECT of
# functions alone survives a non-JSON payload, and the comment on the
# log-failure alarm is corrected to say what was seen. Neither is silence.
# Record all three here, with the form principal() really returned, and
# change PENDING to the date.
#
# What this file does not cover, plainly:
#   - The two roles themselves under a compromised credential. A publish by
#     scoreboard-api or scoreboard-director is excluded whatever caused it,
#     because those roles publish on every game change and this alarm cannot
#     tell a stolen session from a scheduled one. That path is the token
#     mismatch alarm (admin.tf), HockeyTrack's direct-invoke rule (section
#     12) and the IAM change detection.
#   - A policy widened to allow a retained publish on some OTHER topic. The
#     rule listens to scoreboard/+/config and nothing else; hockeytrack/games
#     is the reducer's and is not watched here.
#   - The rule itself being disabled, replaced or deleted. That is an IoT
#     control-plane write, which HockeyTrack's section 7 rule pages on; this
#     file adds no second watch on it.
#   - This log group's retention or metric filter being changed: not on
#     HockeyTrack's section 13 list yet, as the group's comment says. SCO-73.
#   - A publish whose payload the rule engine cannot parse is covered, but
#     only by the log-failure alarm's ParseError term, which says that a
#     non-JSON config arrived and not who sent it: the rule never ran, so
#     there is no line and no principal. The investigation starts from the
#     retained message on each panel's config topic instead. Whether that
#     path is ever taken -- whether a SELECT of functions alone parses a
#     non-JSON payload or not -- is what the verification's third publish
#     observes.
