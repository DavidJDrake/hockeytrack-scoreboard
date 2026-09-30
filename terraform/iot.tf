# Fleet indexing, for one fact: when each panel last connected (SCO-33).
# terraform/sweep.tf carries the decision against the alternative, an IoT
# rule writing a stamp into the devices table. This is account-wide, one
# index named AWS_Things by the service, and the account had it OFF before
# this (`aws iot get-indexing-configuration`, 2026-09-30). REGISTRY is the
# least thing indexing that STATUS can be enabled alongside; no shadow, no
# Device Defender, no named shadows. It is read by two roles, the sweep's
# and the API's, each through iot:SearchIndex on this index and nothing else.
#
# Cost, from the Device Management pricing page (2026-09-30): $2.25 per
# million index updates and $0.05 per ten thousand queries. A fleet of a
# handful of panels updates the index on each connect and disconnect and is
# queried once a day by the sweep and once per admin page load: cents a
# month. Check Cost Explorer after the first bill rather than trust this.
#
# The index does not know a panel's past. A panel already off the broker for
# an hour when this is applied appears as connected=false with no timestamp,
# and stays that way until it next connects; the sweep treats that as
# unknown, never as old, so the first year of the backstop is counted from
# the day a panel connects after this apply, not from the apply.
resource "aws_iot_indexing_configuration" "fleet" {
  thing_indexing_configuration {
    thing_indexing_mode              = "REGISTRY"
    thing_connectivity_indexing_mode = "STATUS"
  }
}

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
