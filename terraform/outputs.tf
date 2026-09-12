output "iot_endpoint" {
  value = data.aws_iot_endpoint.data.endpoint_address
}

output "device_policy_name" {
  value = aws_iot_policy.device.name
}

output "games_table" {
  value = aws_dynamodb_table.games.name
}

output "api_endpoint" {
  value = aws_apigatewayv2_api.admin.api_endpoint
}

output "user_pool_id" {
  value = aws_cognito_user_pool.admin.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.site.id
}
