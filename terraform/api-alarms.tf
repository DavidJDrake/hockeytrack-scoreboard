# Alarms on the admin API (SCO-21).
#
# admin.tf builds an internet-facing HTTP API: a JWT authorizer in front of
# scoreboard-api, and enroll.tf hangs scoreboard-enroll off the same API with
# two unauthenticated routes and the authenticated claim route. Until this
# file, the only alarms on any of it were the three route-scoped ones in
# enroll.tf (flood, claim 4xx, claim 5xx) and the token-mismatch alarm in
# admin.tf. Nothing watched the stage as a whole: a scanner walking the
# authorizer, or the API function failing on every request, signaled nowhere.
#
# The split follows iot-alarms.tf. Anything that looks like somebody trying
# to get in goes to hockeytrack-security-alerts (data.aws_sns_topic
# .security_alerts, iot-alarms.tf) and carries no ok_actions: a probe that
# stops is not news, and the first page has already started the
# investigation. Anything that means the API is broken for its owners goes to
# the ops topic (data.aws_sns_topic.alerts, dlq.tf) and does carry
# ok_actions, because "it recovered" is worth knowing when nobody has touched
# it yet. Every alarm treats missing data as notBreaching: this API sees a
# few requests a day, so an empty period is the normal state, not a fault.
#
# Evidence gathered 2026-09-30 UTC (read-only, us-east-1):
#   - `aws cloudwatch list-metrics --namespace AWS/ApiGateway` for this API's
#     id shows 4xx, 5xx, Count, Latency, IntegrationLatency and DataProcessed
#     under both {ApiId} and {ApiId, Stage}. The stage-level pair is what
#     sections 1 and 3 read: it is emitted by default for an HTTP API, unlike
#     the per-route metrics enroll.tf depends on, which exist only for the
#     one route admin.tf turned detailed metrics on for.
#   - The access log (/aws/apigateway/scoreboard-admin) is one JSON object
#     per line in the shape admin.tf's access_log_settings.format declares,
#     and `status` arrives as a quoted string ("401"), because the format
#     quotes the $context.status placeholder. `aws logs filter-log-events
#     --filter-pattern '{ $.status = "401" }'` returned every real 401 line
#     in the retained window (17 of them, across GET /api/devices, POST
#     /api/devices/claim and four other routes), and the unquoted form
#     matched the same lines. Section 2 uses the quoted form because that is
#     what the log actually holds. The group also carries two non-JSON
#     "Permissions are set correctly" lines API Gateway writes when logging
#     is enabled; a JSON filter skips them.
#   - `aws cloudwatch list-metrics --namespace AWS/Lambda` for scoreboard-api
#     shows Errors and Throttles, which sections 4 and 5 read.
#
# What this file does not do, plainly. There is no WAF and no per-IP rate
# limit; the stage throttle in admin.tf (20 rps, 40 burst) is shared by every
# caller, so it caps how fast an abuser can go but does not single them out,
# and the enroll_flood alarm in enroll.tf records the position that a WAF is
# what gets added the day these alarms show it is needed. Nothing here counts
# requests that succeed: a stolen, still-valid token used to read the owner's
# panels produces 200s and is invisible to every alarm below. And none of
# these alarms can see their own inputs removed: a metric filter or log group
# deleted from under one is invisible to the alarm it fed, because missing
# data is notBreaching, so the alarm goes quiet rather than into ALARM. The
# deletion itself is not invisible. HockeyTrack's hockeytrack-sec-scoreboard-
# support rule (security-alarms.tf, section 13) pages on every logs write
# except CreateLogStream that names /aws/lambda/scoreboard-* or
# /aws/apigateway/scoreboard-admin, and DeleteMetricFilter, PutMetricFilter,
# DeleteLogGroup and PutRetentionPolicy all carry the group name; its
# hockeytrack-sec-alerting-modification rule (section 9) pages when one of
# this stack's "scoreboard-" alarms is rewritten (iot-alarms.tf explains the
# prefix). What nobody sees is a filter left in place with its pattern edited
# to match nothing, and only if that edit arrives through a call section 13
# does not name -- PutMetricFilter is named, so today even that pages. The
# residual is a future logs API that reaches the group through a field
# section 13 does not list, which is the silent-failure mode that rule
# records for itself.

# --- 1. A burst of client errors across the whole stage --------------------
#
# Reads the stage-wide 4xx metric. A scanner walking the API sees 401 from
# every authorized route, 404 from every path that is not a route (which
# never reaches a Lambda, so no function log records it), and 429 once the
# stage throttle engages -- all of which land here and nowhere else in one
# place. The threshold is above anything an innocent source produces: an
# admin's stale browser tab yields one 401 per request the page makes; a
# panel whose enrollment expired gets a 404 on GET /api/enroll and re-submits,
# backing off to one poll every 30 seconds (device/scoreboard/enroll.py,
# BACKOFF), so even a panel that never backed off would send about 60 in
# five minutes; and this API sees a few requests a day in total. More than
# 100 in five minutes is a client that keeps going after being refused. It
# is deliberately a coarse net: sections 2 and 6 catch the specific shapes
# (authorizer rejections, guessed pairing codes) at lower thresholds, and
# enroll_claim_failures in enroll.tf already watches the claim route alone.
resource "aws_cloudwatch_metric_alarm" "api_4xx_burst" {
  alarm_name          = "scoreboard-api-4xx-burst"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 100
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/ApiGateway"
  metric_name         = "4xx"
  dimensions = {
    ApiId = aws_apigatewayv2_api.admin.id
    Stage = aws_apigatewayv2_stage.default.name
  }
  alarm_description  = <<-EOT
    More than 100 4xx responses from the admin API in five minutes, across
    every route. Normal traffic is a few requests a day; this is a scanner or
    a looping client. Read /aws/apigateway/scoreboard-admin for the window:
    each line carries the source ip, the route (or "-" for a path that is not
    one), the status and the caller's sub if a token was accepted. Many
    routes from one ip is a scan; many 429s is a flood that the stage
    throttle is absorbing; many 404s on GET /api/enroll from one ip is a
    panel stuck re-enrolling. There is no per-IP block to apply -- see the
    enroll_flood alarm in enroll.tf for the position on adding a WAF.
  EOT
  alarm_actions      = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data = "notBreaching"
}

# --- 2. The authorizer refusing tokens: 401 specifically --------------------
#
# A 401 from this API is almost always the JWT authorizer refusing the
# Authorization header: missing, malformed, expired, wrong issuer or wrong
# audience. It is produced before any Lambda runs, so it appears in the
# access log and the stage 4xx count and nowhere else. The one other source
# is the functions themselves: cmd/api and cmd/enroll's claim verify the ID
# token again (internal/idtoken) and answer 401 when they reject one the
# authorizer passed. Those lines land in the same access log with a sub
# filled in, so this filter counts them too, which is harmless: admin.tf's
# scoreboard-token-mismatch pages at one on the same event, and every real
# 401 in the retained window was an authorizer refusal. Otherwise 401 is what
# credential stuffing or a
# scanner probing for an unauthenticated route looks like here, and it is
# worth its own, lower threshold than section 1 because the innocent sources
# are so few: a signed-in admin's expired session yields one 401 per request
# the page makes with the dead token, a handful, and the panel never sends a
# JWT at all (its routes are unauthenticated, or use its own bearer token,
# which the function checks). Twenty in five minutes is
# somebody sending tokens the pool did not issue, or the same dead token over
# and over. The authorizer here never produces a 403: no route sets scopes,
# and that is the only condition under which a JWT authorizer does.
#
# The filter is a JSON pattern on the access log, not a Lambda log: the
# function is not invoked for a refused token. It assumes the access-log
# format in admin.tf keeps a `status` field; renaming that field silences
# this alarm without tripping it.
resource "aws_cloudwatch_log_metric_filter" "api_unauthorized" {
  name           = "scoreboard-api-unauthorized"
  log_group_name = aws_cloudwatch_log_group.api_access.name
  pattern        = "{ $.status = \"401\" }"

  metric_transformation {
    name      = "ApiUnauthorized"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "api_unauthorized" {
  alarm_name          = "scoreboard-api-unauthorized"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 20
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "ApiUnauthorized"
  alarm_description   = <<-EOT
    Twenty or more requests to the admin API were refused by the JWT
    authorizer (401) within five minutes. A signed-in admin whose session
    expired produces a handful at most; this is a client presenting tokens
    the scoreboard-admins pool did not issue, or replaying one it did. Read
    /aws/apigateway/scoreboard-admin for status 401 in the window: the source
    ip and route are there, and sub is "-" because the authorizer accepted no
    token. A 401 line that does carry a sub is the function itself rejecting
    a token the authorizer passed: that is the scoreboard-token-mismatch case,
    and its runbook in admin.tf applies.
    One ip across many routes is a scan. If the ip is the owner's, check
    whether the site's token refresh is broken before assuming an attack.
    There is no per-IP block to apply; see enroll.tf's enroll_flood alarm.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

# --- 3. Server errors across the whole stage --------------------------------
#
# Operational, not security: the API answering 5xx means the owner cannot
# manage their panels, whoever is asking. Both functions return their 500s
# as successful Lambda responses (respond() in cmd/api and cmd/enroll always
# returns a nil error), so section 4's Errors metric never sees a "lookup
# failed" from DynamoDB or a refused IoT publish; only the HTTP status does.
# This metric also counts what API Gateway itself produces when a function
# cannot be invoked -- a Lambda throttle, a timeout past admin.tf's 10
# seconds, a broken lambda_permission -- for either function, so it is the
# one alarm that sees scoreboard-enroll being throttled, which section 5
# does not watch. Any 5xx at all is worth a page at this traffic level: the
# only expected one is a 503 from cmd/enroll's claim when the Cognito
# signing keys cannot be fetched, and that is worth knowing too.
#
# enroll_claim_errors in enroll.tf watches the claim route's 5xx alone and
# pages the security topic; a claim 500 therefore pages both, which is
# accepted -- the claim route is the one that mints certificates.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "scoreboard-api-5xx"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions = {
    ApiId = aws_apigatewayv2_api.admin.id
    Stage = aws_apigatewayv2_stage.default.name
  }
  alarm_description  = <<-EOT
    The admin API returned a 5xx. Read /aws/apigateway/scoreboard-admin for
    the route and the error field, then the function's log:
    /aws/lambda/scoreboard-api for the panel and settings routes,
    /aws/lambda/scoreboard-enroll for /api/enroll and /api/devices/claim.
    "lookup failed" is DynamoDB refusing a read (check the role's policy and
    the table's status); a 503 from claim is the Cognito signing keys being
    unreachable; a 5xx with no function log line at all is API Gateway
    failing to invoke the function (throttle, timeout or permission).
  EOT
  alarm_actions      = [data.aws_sns_topic.alerts.arn]
  ok_actions         = [data.aws_sns_topic.alerts.arn]
  treat_missing_data = "notBreaching"
}

# --- 4. The API function failing to run ------------------------------------
#
# Lambda's Errors metric counts invocations the runtime reported as failed:
# a panic, a timeout, a process that died at startup. Because cmd/api reports
# every application error as a nil-error response with an HTTP status, this
# metric is not polluted by "the request was bad" the way authgate's is
# (signin.tf explains why that function needs a log filter instead), so the
# standard metric is the honest signal here and one datapoint is enough.
# Only scoreboard-api is watched: scoreboard-enroll's failures surface as
# 5xx through section 3, and its own log-level failure pattern is a separate
# decision. Ops topic: a crashing function is broken, not attacked.
resource "aws_cloudwatch_metric_alarm" "api_lambda_errors" {
  alarm_name          = "scoreboard-api-lambda-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  alarm_description   = <<-EOT
    scoreboard-api crashed or timed out. The handler answers its own errors
    with an HTTP status and a nil error, so this is the runtime's verdict:
    read /aws/lambda/scoreboard-api for a panic, "Task timed out" (the
    timeout is 10 seconds; the IoT publish and the DynamoDB calls are the
    slow paths) or a startup exit (a missing environment variable). Owners
    see a 5xx for each of these; section 3 of api-alarms.tf pages on that
    too.
  EOT
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
  ok_actions          = [data.aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}

# --- 5. The API function being throttled -----------------------------------
#
# A throttled invocation never starts, writes no log line, and is counted in
# neither Invocations nor Errors; only this metric sees it (the same reasoning
# as authgate_throttles in signin.tf). The function has no reserved
# concurrency, so a throttle means the account's shared concurrency is
# exhausted by one of the other projects in it, or somebody set a reserved
# concurrency of zero on this function. Ops topic, with recovery: the
# owner's next click works again once the pressure is gone.
resource "aws_cloudwatch_metric_alarm" "api_lambda_throttles" {
  alarm_name          = "scoreboard-api-lambda-throttles"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  alarm_description   = <<-EOT
    scoreboard-api was throttled: API Gateway could not start the function,
    and the owner saw a 5xx with no function log line. Check `aws lambda
    get-function-concurrency --function-name scoreboard-api` (it should have
    none) and the account's ConcurrentExecutions metric for whichever
    function in this account is consuming the shared limit.
  EOT
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
  ok_actions          = [data.aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}

# --- 6. Pairing codes that did not match ------------------------------------
#
# cmd/enroll's claim handler answers every refusal with the same 404 so that
# a caller learns nothing, and logs one line, "claim refused", with a reason
# -- unknown code, expired, owner mismatch, already claimed -- and nothing
# else: not the code, not the caller. This filter counts those lines. It is
# a sharper signal than enroll_claim_failures in enroll.tf, which reads the
# route's 4xx metric and so also counts a request with no code (400) and a
# refused token (401): a "claim refused" line means a signed-in, invited user
# presented a code, and it was not theirs to use. Pairing codes are eight
# characters and rotate, so guessing one takes many attempts; a person
# mistyping the one on their screen takes one or two. Five in five minutes is
# somebody working the code space, or an invited user trying a pre-bound
# panel they were not named on -- the reason field tells those apart. The
# rate limit on claims the design asked for is still not built; this alarm is
# the detection that stands in for it, not a control that slows the caller.
#
# The pattern is a quoted phrase, not a JSON filter, because the function
# writes Lambda's default text log format (slog through the log package, as
# admin.tf's token-mismatch filters assume). The line is not in the access
# log: correlate by time with POST /api/devices/claim status 404 there, which
# carries the ip and sub.
resource "aws_cloudwatch_log_metric_filter" "claim_refused" {
  name           = "scoreboard-claim-refused"
  log_group_name = aws_cloudwatch_log_group.enroll.name
  pattern        = "\"claim refused\""

  metric_transformation {
    name      = "ClaimRefused"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "claim_refused" {
  alarm_name          = "scoreboard-claim-refused"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 5
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "ClaimRefused"
  alarm_description   = <<-EOT
    Five or more pairing-code claims were refused within five minutes. Each
    refusal is a signed-in user presenting a code that did not match. Read
    /aws/lambda/scoreboard-enroll for the "claim refused" lines: "unknown
    code" or "expired" repeated is somebody guessing; "owner mismatch" is an
    invited user trying a panel that was pre-bound to someone else; "already
    claimed" is a code being replayed. Then read
    /aws/apigateway/scoreboard-admin for POST /api/devices/claim status 404
    in the same window, which carries the caller's sub and ip. No code was
    consumed by a refusal: the real owner can still claim. There is no
    per-account or per-IP limit on claims to tighten; that is the gap this
    alarm makes visible.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}
