output "iot_endpoint" {
  value = data.aws_iot_endpoint.data.endpoint_address
}

output "device_policy_name" {
  value = aws_iot_policy.device.name
}

output "games_table" {
  value = aws_dynamodb_table.games.name
}
