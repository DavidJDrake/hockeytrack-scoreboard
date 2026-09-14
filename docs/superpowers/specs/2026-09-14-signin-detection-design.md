# Detection for the Sign-in Gate — Design

**Status:** accepted (design approved in conversation, 2026-09-14)
**Date:** 2026-09-14
**Closes:** the two known gaps in `2026-09-13-google-sign-in-design.md` §4.6.
**Repositories:** both. The EventBridge rule and the alarm-name coverage go in
HockeyTrack (`terraform/security-alarms.tf`); the gate's own alarms go here.
Two pull requests, one per repository.

## 1. Purpose

The sign-in gate (`scoreboard-authgate` plus the Cognito pool's triggers) decides
who can reach the admin site, and through it, who owns a panel. It is well
tested and verified on the live stack. Nothing, however, notices if someone
changes the gate itself, or if the gate stops running without logging why.
Both fail quietly today:

- **Changes to the gate go unalerted.** A write that drops the triggers, rewrites the function, or adds an address to the
  invite list goes unnoticed.
- **Silent failures go uncounted.** A crash, a throttle or a timeout refuses sign-in without writing
  a `sign-in refused` line, so the refusal alarm cannot count it.

## 2. What the gate can be defeated or blinded by

| Route | Effect | CloudTrail write it has to make |
|---|---|---|
| Drop the pool's triggers | **Fails open**: every Google account admitted | `UpdateUserPool` |
| Replace the function's code, or point `ALLOWLIST_PARAMETER` at a parameter the attacker controls | Gate admits whoever the attacker chooses | `UpdateFunctionCode*`, `UpdateFunctionConfiguration*` |
| Add an address to the invite list | That address is admitted | `PutParameter` |
| Add an identity provider the attacker controls | The gate admits any `PreSignUp_ExternalProvider` with `email_verified` `"true"`, so a rogue OIDC provider can present the owner's address, verified | `CreateIdentityProvider`, `UpdateIdentityProvider` |
| Add or widen an app client (password flows, `aws.cognito.signin.user.admin`) | A second door the site client's settings do not govern | `CreateUserPoolClient`, `UpdateUserPoolClient` |
| Change a user directly | Rewrite an address or mark it verified | `AdminUpdateUserAttributes`, `AdminCreateUser`, … |
| Remove the function's permission, delete it, or delete the parameter | Fails closed: nobody can sign in | `RemovePermission*`, `DeleteFunction*`, `DeleteParameter(s)` |
| Throttle it or crash it | Fails closed, without a refusal line | none: runtime behavior |

Every route in the first seven rows is a management write that names one of
three resources. The last row is not an API call at all.

## 3. Facts established by investigation (2026-09-14, read-only)

From CloudTrail event history for the deploy and verification the day before:

1. **Every Cognito configuration and admin write names the pool.**
   `UpdateUserPool`, `UpdateUserPoolClient`, `CreateIdentityProvider` and
   `AdminDeleteUser` all carry `requestParameters.userPoolId`.
2. **Sign-in traffic does not.** The hosted-UI events recorded for each sign-in
   (`Token_POST`, `OAuth2Response_GET`, `Logout`, with `readOnly` false) carry
   no `requestParameters` at all. So a pattern on `userPoolId` does not page
   on every sign-in. `InitiateAuth` and `SignUp` carry `clientId`, not
   `userPoolId`, and they are attempts against the gate, not changes to it.
3. **Lambda names the function two ways.** `requestParameters.functionName` is
   the bare name (`scoreboard-authgate`) on most events and the full ARN on
   others (`GetFunctionRecursionConfig`). Tagging calls use `resource`, an ARN.
4. **SSM names the parameter in `name`.** `PutParameter` carries
   `requestParameters.name`; `DeleteParameters` takes a list, `names`.
5. **CloudTrail masks secrets in these events.** `PutParameter`'s `value` is
   hidden, and `CreateIdentityProvider`'s `providerDetails.client_secret` is
   hidden, so this rule's input never carries the Google client secret or an
   invited address.
6. **Every refusal is a Lambda error.** The gate refuses by returning an error,
   and yesterday's refusals each logged
   `{"errorMessage":"this account is not invited",…}`. The `Errors` metric
   therefore counts refusals, and an `Errors` alarm would duplicate the
   refusal alarm rather than detect a crash.
7. **Only `scoreboard-iot-*` alarms are watched for rewriting.** HockeyTrack's
   `hockeytrack-sec-alerting-modification` rule matches alarm names by the
   prefix `scoreboard-iot-`, so `scoreboard-signin-refused` and the three
   `scoreboard-enroll-*` security alarms can be rewritten without a page.

## 4. Design

### 4.1 The rule: `hockeytrack-sec-scoreboard-signin` (HockeyTrack)

It is added to `security_rules` in HockeyTrack's `terraform/security-alarms.tf`,
so it inherits what every rule there has: the SNS target on the
`hockeytrack-security-alerts` topic, the dead-letter queue, the phone-readable
input transformer, and coverage by `alerting_tampering` (deletion) and
`alerting_modification` (rewriting), which match rules by the `hockeytrack-sec`
prefix. The IoT tampering rule, which also defends this project, set the
precedent for putting it there.

The pattern matches any write that names one of the gate's three resources,
and lists no event names:

```hcl
event_pattern = jsonencode({
  "detail-type" = ["AWS API Call via CloudTrail"]
  "detail" = {
    "eventSource" = ["cognito-idp.amazonaws.com", "lambda.amazonaws.com", "ssm.amazonaws.com"]
    "readOnly"    = [false]
    "$or" = [
      { "requestParameters" = { "userPoolId"   = [local.scoreboard_pool_id] } },
      { "requestParameters" = { "functionName" = [{ "wildcard" = "*scoreboard-authgate*" }] } },
      { "requestParameters" = { "resource"     = [{ "wildcard" = "*:function:scoreboard-authgate*" }] } },
      { "requestParameters" = { "name"         = ["/scoreboard/allowed-emails"] } },
      { "requestParameters" = { "names"        = ["/scoreboard/allowed-emails"] } },
      { "requestParameters" = { "resourceId"   = ["/scoreboard/allowed-emails"] } },
      { "requestParameters" = { "resourceArn"  = [local.scoreboard_pool_arn, local.allowlist_parameter_arn] } },
    ]
  }
})
```

- **Fields from the service models, not only from observed events.** Planning
  swept every non-read operation in the cognito-idp, lambda and ssm service
  models (aws-cli 2.33.2) for input fields that name these resources, and found
  three the observed events did not show: Cognito tagging's `resourceArn` (the
  pool), SSM tagging's `resourceId` and SSM resource policies' `resourceArn`
  (the parameter). They are in the pattern.
- **No event-name list.** A misspelled CloudTrail name cannot hide a route, and
  an API AWS adds later alerts the first time it is used. That is the same
  fail-loud reasoning as the IoT rule, and it sidesteps the `$or`
  key-order trap its section 8 records, because `eventName` appears nowhere.
- **Scoped by resource, not by service.** LitLibrary and HealthTracker run
  Cognito pools, Lambdas and SSM parameters in this account. A service-wide
  rule would page on their work.
- **The wildcard over-matches on purpose.** `*scoreboard-authgate*` also matches
  a hypothetical `scoreboard-authgate-v2` or a qualified `scoreboard-authgate:1`.
  Over-matching costs an alert; under-matching costs the detection.
- **The pool ID is resolved, not typed.** A `data "aws_cognito_user_pools"`
  looks the pool up by its name, `scoreboard-admins`, with a precondition that
  exactly one pool matches. If the pool is ever replaced, the next HockeyTrack
  plan either picks up the new ID or fails loudly. It never silently watches an
  ID that no longer exists.
- **The pattern is length-checked.** The same `<= 2048` precondition as
  `alerting_modification`, because EventBridge rejects a longer pattern only at
  apply.

Its alert sentence (`security_alert_meaning.scoreboard_signin`): *If this was
not you, assume the scoreboard admin site's sign-in gate may be bypassed. Check
the invite list, the user pool's triggers, app clients, identity providers and
users, and the authgate function's code and environment, against the scoreboard
repository.*

**Accepted noise.** Any scoreboard apply that changes these resources, any
invitation or removal, and any admin action on the pool. All of it is rare and
deliberate, and the person doing it is the person reading the alert. Plans do
not page, because every read carries `readOnly: true`.

### 4.2 Alarm-name coverage (HockeyTrack)

`alerting_foreign_prefix` widens from `scoreboard-iot-` to `scoreboard-`, and
the `alerting_modify` alert sentence says "scoreboard" instead of
"scoreboard-iot". That brings `scoreboard-signin-refused`, the enroll alarms,
`scoreboard-dlq-depth` and the two new alarms below under rewrite detection.
The pattern gets shorter, not longer. The comment in this repository's
`iot-alarms.tf` that names the old prefix is updated to match.

### 4.3 The gate's own alarms (this repository, `terraform/signin.tf`)

- **`scoreboard-authgate-failures`:** a CloudWatch Logs metric filter on
  `/aws/lambda/scoreboard-authgate` for the Lambda runtime's own failure lines,
  which a refusal never produces:
  - a runtime exit (`Runtime.ExitError` / `Runtime exited`);
  - a timeout (`Task timed out`);
  - a Go `panic:`.

  It counts into `Scoreboard/AuthgateFailures`. The alarm fires at 1 or more in 5
  minutes and goes to the security-alerts topic. Metric math on
  `Errors − SignInRefused` was considered and rejected: a refusal's error and
  its log line can fall on opposite sides of a 5-minute boundary, which would
  page on nothing.
- **`scoreboard-authgate-throttles`:** the `AWS/Lambda` `Throttles` metric for
  the function, 1 or more in 5 minutes, to the same topic. A throttled
  invocation reaches no log, so a metric is the only way to see it.

Both alarm descriptions say what failing closed means here: nobody, the owner
included, can sign in until it clears.

## 5. Testing: prove each alert by making it fire

A pattern that `terraform validate` accepts can still match nothing, so each
route is proven with a harmless real event, the standard HockeyTrack's HOC-56
set. The user runs the writes; the controller reads the results.

| Branch | Harmless trigger | Expected |
|---|---|---|
| SSM `name` | Re-put the invite list's current value (`put-parameter --overwrite`, value from a private file, never printed) | security email naming `PutParameter` |
| Lambda `functionName` | `update-function-configuration` with the timeout it already has (5) | email naming `UpdateFunctionConfiguration20150331v2` |
| Lambda `resource` | `tag-resource`, then `untag-resource`, on the function | two emails |
| Cognito `userPoolId` | `create-group`, then `delete-group`, on the pool (groups are unused) | two emails |
| Negative | a scoreboard `terraform plan` and a normal sign-in | no email |
| Crash alarm | Set `ALLOWLIST_PARAMETER` to empty, so the function exits at startup; attempt one sign-in (refused, fail closed); restore the value | `scoreboard-authgate-failures` in ALARM, email, then OK |
| Throttle alarm | `put-function-concurrency 0`; attempt one sign-in; `delete-function-concurrency` | `scoreboard-authgate-throttles` in ALARM, email |
| Rewrite coverage | re-save `scoreboard-signin-refused` with its current definition (`put-metric-alarm`, values read back first) | the alerting-modification email names `scoreboard-signin-refused`, which today pages nobody |

The crash and throttle breaks each leave sign-in failing closed for a minute
or two, and each reverts with one command. The configuration changes they make
also fire §4.1's rule, which is expected, not a failure of the negative test. After every step, a scoreboard
`terraform plan` must show no drift. The exact timeout log line cannot be
induced safely, so its pattern is checked against AWS's documentation, not
against a real event, and the record says so.

## 6. Out of scope

- **The static site.** S3 and CloudFront writes that could serve a
  phishing page from the real domain need their own rule.
- **The gate's IAM role.** An edit to `scoreboard-authgate`'s role can only
  break the gate, making it fail closed, not bypass it. HockeyTrack's identity
  rule deliberately excludes role-policy churn, for the reasons recorded there.
- **Requiring `Google_` usernames in the gate.** This would close the rogue
  identity provider route at the source instead of detecting it. It is worth
  its own small change, and it is noted in §2 as the route this rule most needs
  to catch until then.
- **Activity outside us-east-1**, where none of these resources exist.
- **A second AWS account for the alerting.** A rewrite of this rule's own pattern is caught
  only minutes later, by which time the pattern is the attacker's. That residual
  is the same one HockeyTrack's section 5 already accepts.
