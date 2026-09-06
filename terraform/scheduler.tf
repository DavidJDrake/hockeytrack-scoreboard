resource "aws_scheduler_schedule_group" "main" {
  name = "scoreboard"
}

# NOTE: the trust condition below must key off the schedule GROUP arn
# (schedule-group/<name>), never schedule/<group>/*. Scheduler presents the
# group arn to STS when it assumes this role; a wildcard on schedule/<group>/*
# silently matches nothing and invocations stop without an obvious error.
# This is exactly the bug that caused a 19-hour outage in HockeyTrack itself.
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_scheduler_schedule_group.main.arn]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "scoreboard-scheduler-invoke"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.today.arn }]
  })
}

resource "aws_scheduler_schedule" "today" {
  name                = "scoreboard-today"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "rate(10 minutes)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.today.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
