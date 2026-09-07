data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  topic_arn_prefix = "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/hockeytrack/games"
  logs             = ["logs:CreateLogStream", "logs:PutLogEvents"]
}

resource "aws_iam_role" "reducer" {
  name               = "scoreboard-reducer"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "reducer" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.reducer.arn}:*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.games.arn]
  }
  statement {
    actions   = ["iot:Publish"]
    resources = ["${local.topic_arn_prefix}/*"]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "reducer" {
  role   = aws_iam_role.reducer.id
  policy = data.aws_iam_policy_document.reducer.json
}

resource "aws_iam_role" "today" {
  name               = "scoreboard-today"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "today" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.today.arn}:*"]
  }
  statement {
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.games.arn]
  }
  statement {
    actions   = ["iot:Publish"]
    resources = ["${local.topic_arn_prefix}/today"]
  }
}

resource "aws_iam_role_policy" "today" {
  role   = aws_iam_role.today.id
  policy = data.aws_iam_policy_document.today.json
}
