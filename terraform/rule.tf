# The whole integration with HockeyTrack: one rule on its bus.
resource "aws_cloudwatch_event_rule" "game_events" {
  name           = "scoreboard-game-events"
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source        = ["hockeytrack.poller"]
    "detail-type" = ["nhl.game.status", "nhl.game.clock", "nhl.game.play", "nhl.game.roster", "nhl.game.final"]
  })
}

resource "aws_cloudwatch_event_target" "reducer" {
  rule           = aws_cloudwatch_event_rule.game_events.name
  event_bus_name = var.bus_name
  arn            = aws_lambda_function.reducer.arn
  dead_letter_config {
    arn = aws_sqs_queue.dlq.arn
  }
  retry_policy {
    maximum_retry_attempts       = 4
    maximum_event_age_in_seconds = 300 # a scoreboard event older than 5 minutes is stale
  }
}

resource "aws_lambda_permission" "events_invoke_reducer" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reducer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.game_events.arn
}
