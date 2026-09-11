# AWS IoT Core logging (V2). Without this, a device that fails to connect or
# gets an authorization denial leaves no record anywhere -- the reducer and
# today Lambdas only see traffic that already made it onto the bus, so a
# rejected device is invisible to everything else in this stack.
#
# `aws iot get-v2-logging-options` returned NotConfiguredException before
# this change: logging has never been turned on for this account.

# IoT V2 logging always writes to a CloudWatch log group named exactly
# "AWSIotLogsV2" -- that name is fixed by the service, not something this
# resource block chooses. It's created here, first, with an explicit
# retention: if IoT creates it instead (which it will, the first time
# SetV2LoggingOptions runs, if this group doesn't already exist), the group
# it creates never expires. 90 days matches HockeyTrack's own CloudTrail log
# group retention.
resource "aws_cloudwatch_log_group" "iot" {
  name              = "AWSIotLogsV2"
  retention_in_days = 90
}

# The role IoT assumes to write into that log group. aws:SourceAccount and
# aws:SourceArn are the confused-deputy conditions AWS's own IoT docs
# recommend for this trust relationship (Cross-service confused deputy
# prevention, "AWS IoT role trust policy" example). V2 logging is an
# account-level setting (SetV2LoggingOptions), not a call scoped to one
# IoT resource, so there's no single resource ARN to pin SourceArn to --
# this uses the same account-wildcard form AWS's own general example does
# (arn:aws:iot:<region>:<account>:*), which still confines the assumption
# to IoT acting on behalf of this account, just not one narrower resource.
data "aws_iam_policy_document" "iot_logging_trust" {
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
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "iot_logging" {
  name               = "scoreboard-iot-logging"
  assume_role_policy = data.aws_iam_policy_document.iot_logging_trust.json
}

# Scoped to the one log group above, not "*". This is narrower than AWS's
# own AWSIoTLogging managed policy (which grants these same actions plus
# PutMetricFilter/PutRetentionPolicy/GetLogEvents/DeleteLogStream on
# Resource "*"): PutRetentionPolicy is deliberately left out, since this
# stack sets retention itself and giving IoT that permission would let it
# silently override the group's expiry back to "never" the way it does when
# it creates the group unmanaged. CreateLogGroup is kept even though the
# group already exists above, because it's harmless when scoped to this one
# ARN and its absence turned into an outright logging failure for other
# people who set this up (AWS's docs note failures here show up only as a
# CloudwatchLogs:LogGroupCreationFailed metric, not a visible error).
data "aws_iam_policy_document" "iot_logging" {
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.iot.arn}:*"]
  }
}

resource "aws_iam_role_policy" "iot_logging" {
  role   = aws_iam_role.iot_logging.id
  policy = data.aws_iam_policy_document.iot_logging.json
}

# The actual switch: this is what calls SetV2LoggingOptions. Default level
# is ERROR, the lowest of the five that still captures what this change
# exists for -- AWS's log-level docs define ERROR as "any error that causes
# an operation to fail," with a failed device authentication given as the
# example, and a failed Connect/Subscribe/Publish authorization is the same
# shape of event. INFO is one level up and would additionally log a line for
# every successful connect, subscribe and publish -- given every reducer
# invocation publishes a retained message, that's a log line per game event,
# all noise for this goal. At this scale (one device, a handful of games a
# day) the volume difference is not a real cost concern either way -- INFO
# would still be well under the CloudWatch Logs free ingestion tier -- ERROR
# is chosen because it's the quietest level that still does the job, not
# because INFO would be expensive.
#
# Removing this block does NOT turn logging off: at the pinned provider
# (v5.100.0) the resource's delete is a no-op, so logging stays on with the
# last role and level. Turning it off is a deliberate
# `aws iot set-v2-logging-options --disable-all-logs`, which HockeyTrack's
# IoT security rule alarms on.
resource "aws_iot_logging_options" "main" {
  default_log_level = "ERROR"
  role_arn          = aws_iam_role.iot_logging.arn
}
