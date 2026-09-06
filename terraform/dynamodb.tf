resource "aws_dynamodb_table" "games" {
  name         = "scoreboard-games"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gameId"
  attribute {
    name = "gameId"
    type = "N"
  }
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }
}
