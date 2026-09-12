# Alarms on the AWS/IoT authentication and authorization failure metrics
# (SCO-18).
#
# terraform/iot-logging.tf turns on IoT V2 logging at ERROR, which records a
# denied Connect/Subscribe/Publish once it reaches the message broker's
# authorization check -- but AWS ships Connection.AuthNError log entries
# disabled by default, and neither the pinned provider nor its main branch
# exposes the eventConfigurations needed to turn that on. So a client that
# fails *authentication* -- a revoked or forged certificate presented at
# connect -- leaves no log line anywhere in this stack, ever. The free
# AWS/IoT CloudWatch metrics are emitted regardless of that logging setting,
# which is why alarming on them, not on logs, is the actual detection
# control for a failed-authentication attempt.
#
# Evidence gathered 2026-09-11 UTC (read-only, us-east-1), via
# `aws cloudwatch list-metrics` / `get-metric-statistics --namespace AWS/IoT`
# over the retained 14 days:
#   - This account has emitted exactly 7 AWS/IoT metrics:
#     PublishRetained.{Success,AuthError}, PublishIn.Success,
#     PublishOut.Success, Subscribe.{Success,AuthError}, Connect.Success.
#     Connect.AuthError, PublishIn.AuthError, PublishOut.AuthError and
#     Connection.AuthNError have no data in that window. Subscribe.AuthError
#     has one datapoint (2026-09-11 02:12Z, a deliberate forbidden subscribe
#     from scoreboard-01). PublishRetained.AuthError has 15 (2026-09-07
#     03:13Z, the reducer's retained publishes failing before
#     iot:RetainPublish was granted -- see iam.tf).
#   - Both auth-error metrics that have emitted carry Protocol=MQTT,
#     *including* PublishRetained.AuthError. That's the trap: the reducer
#     Lambda publishes through the IoT data-plane HTTPS API, and its
#     *successful* retained publishes are correctly tagged Protocol=HTTP
#     (list-metrics shows PublishRetained.Success as HTTP-only, and
#     PublishIn.Success/PublishOut.Success split HTTP/MQTT the way you'd
#     expect). But the 15 failures from that exact same reducer code path
#     were tagged MQTT, not HTTP. AWS's own metrics-dimensions doc notes
#     that the sibling metric PublishIn.AuthError isn't supported for HTTP
#     Publish at all, so it's plausible the retained-publish
#     authorization-error path isn't split by protocol the way the success
#     path is, and lands under MQTT regardless of how the client actually
#     connected. Neither of us found this stated outright anywhere; it's
#     inferred from the two datasets above, not confirmed by AWS docs.
#   - Net effect: an alarm hardcoded to Dimensions={Protocol=HTTP} on
#     PublishRetained.AuthError would have missed the actual 2026-09-07
#     incident entirely, despite HTTP being the "correct" protocol for how
#     the reducer talks to IoT. That's the general shape of the dimension
#     trap this file is written to avoid for every metric below, not just
#     PublishRetained -- nothing rules out a client trying
#     MQTT-over-WebSockets, or AWS adding a Protocol value neither of us has
#     seen.
#
# Aggregation choice: every alarm below uses a CloudWatch Metrics Insights
# query (`SELECT SUM("<metric>") FROM "AWS/IoT"`) instead of an alarm pinned
# to Dimensions={Protocol=MQTT}. A bare `FROM "namespace"` matches the metric
# "no matter their dimensions, and returns a single aggregated time series"
# (Metrics Insights query-language doc, FROM clause), and PutMetricAlarm's
# MetricDataQuery.Expression accepts a Metrics Insights query, which is what
# metric_query.expression sends.
#
# That was proven against real data, not just planned -- `terraform plan`
# cannot tell whether AWS will accept a query. `aws cloudwatch
# get-metric-data` with these exact expressions, 2026-09-11: Subscribe.AuthError
# returned the one probe datapoint (02:12Z); Connection.AuthNError parsed and
# returned no data, as it should; and PublishRetained.Success, which exists
# only under Protocol=HTTP, returned 17 -- so the undimensioned query really
# does sum across Protocol values rather than silently reading one.
#
# So every alarm fires whether the failure lands under MQTT, HTTP, or a
# Protocol value AWS adds later. One alarm per known Protocol value would be
# slightly cheaper and would go blind the day a new value appears; for the
# control that detects authentication and authorization bypass, that blind
# spot costs more than the difference. Cost: 6 alarms at $0.10 a month, plus
# a Metrics Insights query charge that at one device's metric volume is small
# -- check Cost Explorer after the first bill rather than trust an estimate.

# These alarm names are load-bearing outside this repository. HockeyTrack's
# hockeytrack-sec-alerting-modification rule pages when a security alarm is
# rewritten rather than deleted, and it finds this stack's alarms by the
# "scoreboard-iot-" prefix, because they publish to its security topic.
# Renaming them here silently drops that coverage: nothing fails, no plan
# shows a difference, and the alarms simply stop being watched. Rename them
# only alongside that rule.
variable "security_alerts_topic_name" {
  type        = string
  default     = "hockeytrack-security-alerts"
  description = "HockeyTrack's security-alerts SNS topic. Owned by that stack, not this one; referenced by name because this repo does not manage it."
}

# Confirmed read-only 2026-09-11 UTC that a same-account CloudWatch alarm can
# publish here without any change to this topic: `aws sns
# get-topic-attributes` shows a statement (Sid "AccountOwner") granting
# SNS:Publish to Principal "*" conditioned on
# StringEquals{aws:SourceOwner=989232581535} -- the standard SNS
# default-policy shape that lets any same-account resource (not just a
# named EventBridge rule) publish. The explicit "SecurityRulesPublish"
# statement only lists HockeyTrack's own hockeytrack-sec-* EventBridge
# rules by ARN, which is what you'd expect for EventBridge (unlike
# CloudWatch alarms, EventBridge always needs to be named explicitly). Three
# existing HockeyTrack CloudWatch alarms already rely on the AccountOwner
# statement with no rule-specific grant of their own:
# hockeytrack-security-root-console-signin, hockeytrack-security-trail-silent
# and hockeytrack-security-archive-shrank all list this topic's ARN as their
# only AlarmAction (`aws cloudwatch describe-alarms
# --alarm-name-prefix hockeytrack-security`), and root-console-signin's own
# StateReason shows CloudWatch evaluating and transitioning it, so the
# publish path is exercised, not just configured.
data "aws_sns_topic" "security_alerts" {
  name = var.security_alerts_topic_name
}

# --- Authentication failure: no log line exists for this anywhere --------

resource "aws_cloudwatch_metric_alarm" "iot_connection_authn_error" {
  alarm_name        = "scoreboard-iot-connection-authn-error"
  alarm_description = <<-EOT
    A client failed authentication connecting to this account's IoT
    endpoint -- a revoked, expired, or forged certificate, or a scan/probe
    presenting an SNI string matching the endpoint. Unlike every
    other alarm in this file, there is NO log line to go read: IoT's
    Connection.AuthNError log entries are disabled by default and this
    stack's logging (iot-logging.tf) has no way to turn them on. This
    metric alarm is the only record that the attempt happened. Start with
    `aws iot list-certificates` for anything unexpected, and CloudTrail
    around the alarm time for UpdateCertificate / CreateCertificateFromCsr /
    AttachThingPrincipal.
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]

  metric_query {
    id          = "authn_errors"
    expression  = "SELECT SUM(\"Connection.AuthNError\") FROM \"AWS/IoT\""
    period      = 60
    return_data = true
  }
}

# --- Authorization failures: logged at ERROR in AWSIotLogsV2 -------------
#
# None of these four carry ok_actions. Each fires on a single failed
# datapoint with a 60-second period and notBreaching missing data, and clears
# by itself -- but not on the next evaluation. With the default sliding
# window, CloudWatch keeps re-reading the last real datapoint while it is
# inside the evaluation range, so a one-off failure holds the alarm in ALARM
# for five minutes: measured 2026-09-11, 03:38:10Z to 03:43:10Z. An OK page
# would say nothing new. The cost worth knowing: an alarm only acts when its
# state changes, so a second failure inside those five minutes does not page
# again. The first page has already started the investigation. PublishRetained.AuthError below is different (ongoing
# operational breakage, not a momentary probe), and does get one.

resource "aws_cloudwatch_metric_alarm" "iot_connect_auth_error" {
  alarm_name        = "scoreboard-iot-connect-auth-error"
  alarm_description = <<-EOT
    A client authenticated successfully but was denied at CONNECT -- its
    certificate is valid but not attached to a policy permitting
    iot:Connect for the client ID it presented. The device name and
    certificate are in the AWSIotLogsV2 CloudWatch log group (this stack's
    IoT logging, ERROR level). Check whether scoreboard-01's certificate or
    policy attachment changed, and whether the client ID matches the thing
    name a legitimate device would use.
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]

  metric_query {
    id          = "connect_auth_errors"
    expression  = "SELECT SUM(\"Connect.AuthError\") FROM \"AWS/IoT\""
    period      = 60
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "iot_subscribe_auth_error" {
  alarm_name        = "scoreboard-iot-subscribe-auth-error"
  alarm_description = <<-EOT
    A client was denied a SUBSCRIBE request -- it asked for a topic filter
    its policy doesn't grant. The device policy (iot.tf) only allows
    subscribing to hockeytrack/games/* and the caller's own
    scoreboard/<ThingName>/config, so this fires on any attempt to read
    another device's config topic or anything outside those two filters.
    The device name, certificate and requested topic filter are in the
    AWSIotLogsV2 CloudWatch log group (ERROR level).
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]

  metric_query {
    id          = "subscribe_auth_errors"
    expression  = "SELECT SUM(\"Subscribe.AuthError\") FROM \"AWS/IoT\""
    period      = 60
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "iot_publish_in_auth_error" {
  alarm_name        = "scoreboard-iot-publish-in-auth-error"
  alarm_description = <<-EOT
    A client was denied publishing a message -- and the device policy
    (iot.tf) grants scoreboard-01 no iot:Publish action at all, on any
    topic. This device only ever subscribes; it should never generate this
    metric. Any datapoint here is a device or client trying to publish
    something it was never meant to, which is anomalous by construction,
    not just a misconfiguration. The device name, certificate and target
    topic are in the AWSIotLogsV2 CloudWatch log group (ERROR level).
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]

  metric_query {
    id          = "publish_in_auth_errors"
    expression  = "SELECT SUM(\"PublishIn.AuthError\") FROM \"AWS/IoT\""
    period      = 60
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "iot_publish_out_auth_error" {
  alarm_name        = "scoreboard-iot-publish-out-auth-error"
  alarm_description = <<-EOT
    The message broker was denied delivering a message OUT to a subscribed
    client -- that client's iot:Receive permission for the topic no longer
    covers a subscription it already holds. Check whether the device policy
    (iot.tf) or a thing's principal/policy attachment changed while a
    client had an active session. The device name and topic are in the
    AWSIotLogsV2 CloudWatch log group (ERROR level).
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]

  metric_query {
    id          = "publish_out_auth_errors"
    expression  = "SELECT SUM(\"PublishOut.AuthError\") FROM \"AWS/IoT\""
    period      = 60
    return_data = true
  }
}

# --- Operational, not security: one of this stack's own Lambdas is broken -
#
# PublishRetained.AuthError means scoreboard-reducer, scoreboard-today, or
# scoreboard-api (the only three identities in this account with any
# iot:Publish grant -- iam.tf, admin.tf) had a retained publish denied.
# That's this stack's own Lambda losing a permission it's supposed to have,
# exactly as happened 2026-09-06/07 before iot:RetainPublish was granted,
# not a third party probing the device policy. It goes to the ops alerts
# topic (dlq.tf), not hockeytrack-security-alerts, and does get ok_actions:
# unlike a probe against the device policy, "the Lambda started working
# again" is useful information on its own, not just a redundant page.
resource "aws_cloudwatch_metric_alarm" "iot_publish_retained_auth_error" {
  alarm_name        = "scoreboard-iot-publish-retained-auth-error"
  alarm_description = <<-EOT
    scoreboard-reducer, scoreboard-today, or scoreboard-api was denied a
    retained publish -- the iot:RetainPublish/iot:Publish grant in iam.tf
    or admin.tf is missing or wrong, or the Lambda's topic changed without
    a matching policy update. Devices reconnecting will not get current
    game state (reducer/today) or a fresh config (api) until this is
    fixed. This metric has been observed tagged Protocol=MQTT even for the
    reducer's HTTPS data-plane calls (see the top of this file) -- don't
    assume Protocol=HTTP and look under the wrong dimension by hand; this
    alarm already aggregates across all of them. The failing role/topic
    are in the AWSIotLogsV2 CloudWatch log group (ERROR level); the
    Lambda's own error is in /aws/lambda/scoreboard-reducer,
    /aws/lambda/scoreboard-today, or /aws/lambda/scoreboard-api.
  EOT

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
  ok_actions          = [data.aws_sns_topic.alerts.arn]

  metric_query {
    id          = "publish_retained_auth_errors"
    expression  = "SELECT SUM(\"PublishRetained.AuthError\") FROM \"AWS/IoT\""
    period      = 60
    return_data = true
  }
}
