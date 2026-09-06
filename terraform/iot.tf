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
        Effect   = "Allow"
        Action   = ["iot:Subscribe"]
        Resource = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topicfilter/hockeytrack/games/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["iot:Receive"]
        Resource = ["${local.topic_arn_prefix}/*"]
      }
    ]
  })
}
