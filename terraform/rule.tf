# The whole integration with HockeyTrack: one rule on its bus.
resource "aws_cloudwatch_event_rule" "game_events" {
  name           = "scoreboard-game-events"
  event_bus_name = var.bus_name
  # hockeytrack.synthetic is HockeyTrack's replay harness: it reconstructs
  # archived games and republishes them to this bus in real time, which is
  # what makes out-of-season development and bench-testing of this scoreboard
  # possible. Do not remove it as dead configuration. Synthetic game ids are
  # the real id plus 9,000,000,000 (11 digits vs. a real game's 10), enforced
  # on the publishing side, so replayed games cannot collide with real ones.
  event_pattern = jsonencode({
    source        = ["hockeytrack.poller", "hockeytrack.synthetic"]
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
