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

# SCO-40. A template is a named set of games that any number of one account's
# panels may share (cloud/internal/templates). The key is what enforces the
# rule the design puts first: hash key owner (the Cognito subject), range key
# templateId (random, made by the server), and no index. Every read is a
# Query under the caller's subject and every write names both halves of the
# key, so there is no way to reach a template by id alone and no ownership
# check that could be forgotten; a template that is not the caller's is one
# the table never returns for them, and the API answers 404, never 403.
#
# Recovery and deletion protection as the devices table: what an owner chose
# cannot be rebuilt from anything else.
resource "aws_dynamodb_table" "templates" {
  name         = "scoreboard-templates"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "owner"
  range_key    = "templateId"

  attribute {
    name = "owner"
    type = "S"
  }
  attribute {
    name = "templateId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
  deletion_protection_enabled = true
}
