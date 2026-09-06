resource "aws_sqs_queue" "dlq" {
  name                      = "scoreboard-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.dlq.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.game_events.arn } }
    }]
  })
}

data "aws_sns_topic" "alerts" {
  name = var.alerts_topic_name
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "scoreboard-dlq-depth"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
}
