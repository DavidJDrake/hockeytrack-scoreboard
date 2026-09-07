# The device policy; things and certificates are created per device by
# tools/provision.sh.
resource "aws_iot_policy" "device" {
  name = "scoreboard-device"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iot:Connect"]
        Resource = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:client/$${iot:Connection.Thing.ThingName}"]
      },
      {
        Effect = "Allow"
        Action = ["iot:Subscribe"]
        Resource = [
          "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topicfilter/hockeytrack/games/*",
          # The device's own config topic, and only its own: the thing-name
          # policy variable is substituted per connection, so one panel can
          # never subscribe to another's. Inbound only -- there is still no
          # iot:Publish anywhere in this policy.
          "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topicfilter/scoreboard/$${iot:Connection.Thing.ThingName}/config",
        ]
      },
      {
        Effect = "Allow"
        Action = ["iot:Receive"]
        Resource = [
          "${local.topic_arn_prefix}/*",
          "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/scoreboard/$${iot:Connection.Thing.ThingName}/config",
        ]
      }
    ]
  })
}
