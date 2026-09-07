data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

data "archive_file" "reducer" {
  type        = "zip"
  source_file = "${path.module}/../build/reducer/bootstrap"
  output_path = "${path.module}/../build/reducer.zip"
}

data "archive_file" "today" {
  type        = "zip"
  source_file = "${path.module}/../build/today/bootstrap"
  output_path = "${path.module}/../build/today.zip"
}

# The log groups' names are literals, not interpolated from the Lambda
# resources below: if they instead read aws_lambda_function.*.function_name,
# Terraform would create each function first and the log group second. On a
# first apply that leaves a window where the function could be invoked (and
# so auto-create AWS's own default, un-retained log group) before Terraform
# creates its explicit one, which then fails to create because the name is
# already taken. depends_on below inverts the order explicitly so the log
# group always exists before its function can run.
resource "aws_cloudwatch_log_group" "reducer" {
  name              = "/aws/lambda/scoreboard-reducer"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "today" {
  name              = "/aws/lambda/scoreboard-today"
  retention_in_days = 30
}

resource "aws_lambda_function" "reducer" {
  function_name    = "scoreboard-reducer"
  role             = aws_iam_role.reducer.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.reducer.output_path
  source_code_hash = data.archive_file.reducer.output_base64sha256
  timeout          = 10
  memory_size      = 128
  environment {
    variables = {
      GAMES_TABLE  = aws_dynamodb_table.games.name
      IOT_ENDPOINT = "https://${data.aws_iot_endpoint.data.endpoint_address}"
    }
  }
  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }
  depends_on = [aws_cloudwatch_log_group.reducer]
}

resource "aws_lambda_function" "today" {
  function_name    = "scoreboard-today"
  role             = aws_iam_role.today.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.today.output_path
  source_code_hash = data.archive_file.today.output_base64sha256
  timeout          = 30
  memory_size      = 128
  environment {
    variables = {
      GAMES_TABLE  = aws_dynamodb_table.games.name
      IOT_ENDPOINT = "https://${data.aws_iot_endpoint.data.endpoint_address}"
      SCHEDULE_URL = var.schedule_url
    }
  }
  depends_on = [aws_cloudwatch_log_group.today]
}
