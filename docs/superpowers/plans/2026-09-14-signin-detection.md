# Detection for the Sign-in Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Page someone whenever the scoreboard admin site's sign-in gate is changed, crashes, or is throttled.

**Architecture:** An EventBridge rule in HockeyTrack's account-security file matches any CloudTrail write that names the scoreboard's user pool, the `scoreboard-authgate` function, or the invite-list parameter. HockeyTrack's existing rewrite detection widens to every `scoreboard-` alarm. In this repository, a log metric filter on the Lambda runtime's own failure lines and a `Throttles` alarm catch a gate that stops running without logging a refusal.

**Tech Stack:** Terraform (AWS provider 5.100) in two repositories; EventBridge event patterns; CloudWatch Logs metric filters; `node --test` tripwires.

**Spec:** `docs/superpowers/specs/2026-09-14-signin-detection-design.md` (in hockeytrack-scoreboard)

## Global Constraints

- Two repositories, both PUBLIC: `/home/jay/projects/hockeytrack` (branch `scoreboard-signin-detection`) and `/home/jay/projects/hockeytrack-scoreboard` (branch `signin-detection`). Each task names its repository. Never work on `main` in either.
- Never commit, edit or delete `terraform/terraform.tfvars` in either repository. Never commit a certificate, key, credential or real email address.
- Subagents run only `terraform fmt` and `terraform validate`. Never `terraform plan`, `apply`, `init`, `console`, `show -json`, any mutating AWS call, `make deploy`, `make site`, or `git push`.
- US spelling.
- The rule's name is `hockeytrack-sec-scoreboard-signin`; its registry key is `scoreboard_signin`. The alarms are `scoreboard-authgate-failures` and `scoreboard-authgate-throttles`, in namespace `Scoreboard` (metric `AuthgateFailures`) and `AWS/Lambda` (metric `Throttles`).
- The resources watched are the pool named `scoreboard-admins`, the function `scoreboard-authgate`, and the parameter `/scoreboard/allowed-emails`, all in us-east-1.
- Every new rule and alarm notifies `hockeytrack-security-alerts`.
- The new rule lists no `eventName`, so section 8's `$or` key-order trap cannot apply.
- Match the surrounding file's style. In HockeyTrack's `security-alarms.tf` that means numbered sections, comments that give evidence and reasons, and a length precondition on every `$or` pattern.
- Commits end with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i
  ```
- Terraform needs `export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime"` first. Both repositories already have `terraform/.terraform` initialized.

---

## File Structure

| Repository | File | Change |
|---|---|---|
| hockeytrack | `terraform/security-alarms.tf` | New section 10 (the rule, its data source and preconditions); registry entry and alert sentence; section 9's alarm prefix widened |
| hockeytrack | `docs/threat-model.md` | A §4 paragraph for the new rule; a §7 recovery entry; the prefix wording |
| hockeytrack-scoreboard | `terraform/signin.tf` | The failures metric filter and alarm; the throttles alarm |
| hockeytrack-scoreboard | `terraform/iot-alarms.tf` | The load-bearing-names comment now names `scoreboard-` |
| hockeytrack-scoreboard | `site/tests/signin-config.test.js` | Tripwires: the failures pattern never matches a refusal; the gate alarms notify the security topic; every alarm carries the `scoreboard-` prefix |
| hockeytrack-scoreboard | `docs/superpowers/specs/2026-09-13-google-sign-in-design.md` | §4.6 marked closed |

---

### Task 1: The rule (hockeytrack)

**Files:**
- Modify: `/home/jay/projects/hockeytrack/terraform/security-alarms.tf`
- Modify: `/home/jay/projects/hockeytrack/docs/threat-model.md`

**Interfaces:**
- Consumes: `data.aws_caller_identity.current` (terraform/data.tf), `var.region`, `local.security_rules`, `local.security_alert_meaning`. Adding a key to both maps automatically gives the rule an SNS target, a dead-letter queue, a place in the topic policy and the queue policy, and an alert template.
- Produces: `aws_cloudwatch_event_rule.scoreboard_signin` (named `hockeytrack-sec-scoreboard-signin`) and `local.scoreboard_signin_pattern`, which Task 4 renders and tests.

Before editing, create the branch: `cd /home/jay/projects/hockeytrack && git checkout -b scoreboard-signin-detection`. Expected: on `main`, with a clean tree, before branching.

- [ ] **Step 1: Register the rule and its alert sentence**

In `terraform/security-alarms.tf`, replace:

```hcl
    iot             = aws_cloudwatch_event_rule.iot_tampering
    logs            = aws_cloudwatch_event_rule.audit_log_tampering
  }
```

with:

```hcl
    iot               = aws_cloudwatch_event_rule.iot_tampering
    logs              = aws_cloudwatch_event_rule.audit_log_tampering
    scoreboard_signin = aws_cloudwatch_event_rule.scoreboard_signin
  }
```

In `security_alert_meaning`, after the line that begins `    logs            = "If this was not you, assume audit history has been destroyed`, add:

```hcl
    scoreboard_signin = "If this was not you, assume the scoreboard admin site's sign-in gate may be bypassed. Check the invite list, the user pool's triggers, app clients, identity providers and users, and the authgate function's code and environment, against the scoreboard repository."
```

Then run `terraform fmt` so both maps' `=` signs realign.

- [ ] **Step 2: Add section 10 at the end of the file**

Append to `terraform/security-alarms.tf`:

```hcl

# ---- 10. The scoreboard admin site's sign-in gate ----
#
# The scoreboard's admin site signs people in with Google, and only invited
# addresses get an account. What enforces that is not IAM and not Cognito's own
# settings. It is one Lambda, scoreboard-authgate, which the user pool calls as
# its pre sign-up and pre token generation triggers, reading the invite list
# from one SSM parameter. The pool's allow_admin_create_user_only was verified
# live NOT to stop federated sign-up, so the triggers are the control. That
# makes three resources an authorization root, and until this rule a write to
# any of them paged nobody:
#
#   Drop the pool's triggers                -> UpdateUserPool. Fails OPEN:
#                                              every Google account admitted.
#   Rewrite the function, or point its      -> UpdateFunctionCode*,
#   ALLOWLIST_PARAMETER elsewhere              UpdateFunctionConfiguration*.
#   Invite yourself                         -> PutParameter.
#   Add an identity provider you control    -> CreateIdentityProvider. The gate
#                                              admits any external provider
#                                              whose mapping says the address is
#                                              verified.
#   Add or widen an app client              -> Create/UpdateUserPoolClient.
#   Rewrite a user directly                 -> AdminUpdateUserAttributes and
#                                              the other Admin* calls.
#   Stop the gate running                   -> RemovePermission*,
#                                              DeleteFunction*,
#                                              DeleteParameter(s). Fails
#                                              closed, but still unexplained.
#
# Scoped by resource, like section 9, and for the same reason as section 9's
# prefixes: this account also runs LitLibrary's and HealthTracker's user pools,
# Lambdas and parameters, so a service-wide rule would page on their work. Like
# sections 7 and 8, it lists no event names. It matches any write that names
# one of the three resources, in whichever request field names it. A misspelled
# CloudTrail name therefore cannot hide a route, and an API added later alerts
# the first time it is used.
#
# Which fields name them comes from the input shape of every non-read operation
# in the cognito-idp, lambda and ssm service models shipped with aws-cli 2.33.2,
# cased the way CloudTrail records them. That casing was confirmed against real
# events on 2026-09-14 for userPoolId, functionName and name:
#
#   userPoolId    every Cognito configuration and admin write (58 operations)
#   resourceArn   Cognito TagResource/UntagResource (the pool's ARN), and SSM
#                 Put/DeleteResourcePolicy (the parameter's ARN)
#   functionName  every Lambda write that takes a function (30 operations).
#                 CloudTrail records it both bare and as a full ARN, so it is
#                 matched with a wildcard.
#   resource      Lambda TagResource/UntagResource, an ARN
#   name          SSM PutParameter, DeleteParameter, (Un)LabelParameterVersion
#   names         SSM DeleteParameters, a list
#   resourceId    SSM AddTagsToResource/RemoveTagsFromResource
#
# Sign-in traffic does not page. The hosted-UI events recorded for every
# sign-in (Token_POST, OAuth2Response_GET, Logout) carry no requestParameters at
# all, and InitiateAuth and SignUp carry clientId, not userPoolId. Those are
# attempts against the gate, which the scoreboard's refusal alarm counts, not
# changes to it. Plans stay silent because reads are readOnly true. CloudTrail
# masks PutParameter's value and CreateIdentityProvider's client_secret, so no
# invited address and no Google secret reaches this rule's input.
#
# What does page is legitimate change, and that is accepted: a scoreboard apply
# that touches these resources, every invitation or removal, and any admin
# action on the pool. All of it is rare and deliberate, and the person doing it
# is the person reading the alert.
#
# The pool ID is looked up, not typed, because the pool can be replaced (its
# username_attributes forces a new pool). The precondition fails the plan unless
# exactly one pool has this name, so a replaced pool is picked up and a missing
# or duplicated one is refused, instead of the rule silently watching an ID that
# no longer exists. The function and the parameter have fixed names in the
# scoreboard's Terraform and are named here as literals, the way AWSIotLogsV2 is
# in section 8. If that repository renames either, this rule silently stops
# covering it.
#
# What it does not see: the static site's bucket and distribution, which could
# serve a look-alike sign-in page; the gate's IAM role, whose edits can only
# make the gate fail closed; a crash or throttle, which are not API calls and
# which the scoreboard's own authgate alarms watch; and rewriting this rule,
# which section 9 catches.
data "aws_cognito_user_pools" "scoreboard" {
  name = "scoreboard-admins"
}

locals {
  scoreboard_signin_arn_stem  = "${var.region}:${data.aws_caller_identity.current.account_id}"
  scoreboard_signin_parameter = "/scoreboard/allowed-emails"

  scoreboard_signin_pattern = jsonencode({
    "detail-type" = ["AWS API Call via CloudTrail"]
    "detail" = {
      "eventSource" = ["cognito-idp.amazonaws.com", "lambda.amazonaws.com", "ssm.amazonaws.com"]
      "readOnly"    = [false]
      "$or" = [
        { "requestParameters" = { "userPoolId" = data.aws_cognito_user_pools.scoreboard.ids } },
        { "requestParameters" = { "functionName" = [{ "wildcard" = "*scoreboard-authgate*" }] } },
        { "requestParameters" = { "resource" = [{ "wildcard" = "*:function:scoreboard-authgate*" }] } },
        { "requestParameters" = { "name" = [local.scoreboard_signin_parameter] } },
        { "requestParameters" = { "names" = [local.scoreboard_signin_parameter] } },
        { "requestParameters" = { "resourceId" = [local.scoreboard_signin_parameter] } },
        { "requestParameters" = { "resourceArn" = concat(
          data.aws_cognito_user_pools.scoreboard.arns,
          ["arn:aws:ssm:${local.scoreboard_signin_arn_stem}:parameter${local.scoreboard_signin_parameter}"],
        ) } },
      ]
    }
  })
}

resource "aws_cloudwatch_event_rule" "scoreboard_signin" {
  name          = "hockeytrack-sec-scoreboard-signin"
  description   = "Any write naming the scoreboard admin site's user pool, sign-in gate function or invite list: the routes to bypassing or blinding the gate"
  event_pattern = local.scoreboard_signin_pattern

  lifecycle {
    precondition {
      condition     = length(data.aws_cognito_user_pools.scoreboard.ids) == 1
      error_message = "Expected exactly one Cognito user pool named scoreboard-admins, found ${length(data.aws_cognito_user_pools.scoreboard.ids)}. The scoreboard sign-in rule would watch the wrong pool, or none."
    }
    precondition {
      condition     = length(local.scoreboard_signin_pattern) <= 2048
      error_message = "The scoreboard sign-in rule's event pattern is ${length(local.scoreboard_signin_pattern)} characters. EventBridge rejects patterns over 2048, and only at apply."
    }
  }
}
```

- [ ] **Step 3: Format and validate**

Run: `cd /home/jay/projects/hockeytrack/terraform && export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime" && terraform fmt && terraform validate`
Expected: `Success! The configuration is valid.` Review what `fmt` changed. It should only realign `=` signs.

`validate` does not evaluate data sources or preconditions, so it cannot catch a pattern EventBridge would reject. Task 4 renders the pattern and tests it against real events before anything is applied.

- [ ] **Step 4: Threat model §4 — the new structural claim**

In `docs/threat-model.md`, replace:

```markdown
**Destroying the archive is gated, but the gate is honest about its size.**
```

with:

```markdown
**Changing the scoreboard's sign-in gate pages someone.** The scoreboard's
admin site admits only invited Google accounts, and the thing that enforces
that is a Lambda the user pool calls as a trigger, reading the invite list from
one SSM parameter. The pool's own invite-only setting was verified not to stop
Google accounts, so those three resources are an authorization root. A rule
fires on any write that names the pool, the function or the parameter, in
whichever request field names it: dropping the triggers, which fails open to
every Google account; rewriting the function or its configuration; adding an
address to the list; adding an identity provider or an app client; or changing
a user directly. It lists no event names, and it is scoped to the scoreboard's
resources because two other projects run pools, Lambdas and parameters in this
account. The pool is found by name at plan time, so a replaced pool is picked up
rather than silently unwatched. Sign-ins themselves do not page: the per-sign-in
Cognito events carry no request parameters to match. The scoreboard repository
separately alarms on the gate crashing or being throttled, which are not API
calls.

**Destroying the archive is gated, but the gate is honest about its size.**
```

- [ ] **Step 5: Threat model §7 — what to do with the alert**

Replace:

```markdown
**The archive has lost objects.** Do not write anything to the bucket. Every
```

with:

```markdown
**A scoreboard sign-in alert you cannot account for.** Assume someone can admit
an account of their choosing to the scoreboard admin site, and with it claim
panels. The Actor line names the credential, and the root sign-in procedure
applies to it. Then, in us-east-1:
1. `aws cognito-idp describe-user-pool --user-pool-id <id> --query
   'UserPool.LambdaConfig'`. Both `PreSignUp` and `PreTokenGenerationConfig`
   must name `scoreboard-authgate`. If they are missing, the gate is open.
2. `list-user-pool-clients` must show exactly one client, and
   `list-identity-providers` exactly one provider, `Google`.
3. `list-users`: every user should be an invited address, and none should be
   anything but `EXTERNAL_PROVIDER`.
4. `aws lambda get-function --function-name scoreboard-authgate` and
   `get-function-configuration`. Compare the code SHA and
   `ALLOWLIST_PARAMETER` against a fresh `make build` and plan in the
   scoreboard repository.
5. Read the invite list and remove anyone you did not invite. Then run a plan
   in the scoreboard repository: anything rewritten shows as a difference, and
   applying puts it back. The invite list's value is the exception, because
   Terraform deliberately ignores it.

**The archive has lost objects.** Do not write anything to the bucket. Every
```

- [ ] **Step 6: Commit**

```bash
cd /home/jay/projects/hockeytrack
git add terraform/security-alarms.tf docs/threat-model.md
git commit -m "security: page on any change to the scoreboard's sign-in gate

The scoreboard admin site admits only invited Google accounts, and what
enforces that is a Lambda trigger on its user pool reading an SSM invite
list. The pool's own invite-only setting was verified not to stop
federated sign-up, so the pool, the function and the parameter are an
authorization root. A write to any of them paged nobody, including an
UpdateUserPool that drops the triggers and fails open to every Google
account.

Section 10 matches any write naming one of the three, in whichever
request field names it. The field list comes from the cognito-idp, lambda
and ssm service models. It lists no event names, and it is scoped by
resource because two other projects run pools, Lambdas and parameters in
this account. The pool is found by name, with a precondition that exactly
one matches, so a replaced pool cannot leave the rule watching a dead ID.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 2: Rewrite detection covers every scoreboard alarm (hockeytrack)

**Files:**
- Modify: `/home/jay/projects/hockeytrack/terraform/security-alarms.tf` (section 9, plus the `alerting_modify` alert sentence)
- Modify: `/home/jay/projects/hockeytrack/docs/threat-model.md`

**Interfaces:**
- Consumes: Task 1's branch `scoreboard-signin-detection`.
- Produces: `local.alerting_foreign_prefix = "scoreboard-"`. Task 3 relies on this: every scoreboard alarm name starts with `scoreboard-`, and its tripwire test enforces that.

- [ ] **Step 1: Widen the prefix**

In `terraform/security-alarms.tf`, replace:

```hcl
  alerting_foreign_prefix = "scoreboard-iot-"
```

with:

```hcl
  alerting_foreign_prefix = "scoreboard-"
```

- [ ] **Step 2: Correct section 9's comment**

Replace:

```hcl
# single prefix "hockeytrack-sec" covers all three. The scoreboard's six IoT
# authorization alarms are security alarms too -- they publish to this topic --
# but they are named scoreboard-iot-*, so that prefix is named here as a
# literal, the way AWSIotLogsV2 is in section 8. They are owned by the other
# repository; if it renames them, this rule silently stops covering them.
```

with:

```hcl
# single prefix "hockeytrack-sec" covers all three. The scoreboard's alarms are
# security alarms too -- its IoT authorization, enrollment, sign-in refusal and
# sign-in gate alarms all publish to this topic -- and every one is named
# scoreboard-*, so that prefix is named here as a literal, the way AWSIotLogsV2
# is in section 8. It was scoreboard-iot- until 2026-09-14, which left the
# enrollment and sign-in alarms rewritable without a page. They are owned by the
# other repository, whose tests fail if an alarm there loses the prefix; a
# rename that dropped it would otherwise silently end this rule's coverage. The
# wider prefix also covers scoreboard-dlq-depth, which notifies the operational
# topic, and rewriting that pages too; that is accepted.
```

- [ ] **Step 3: Correct the alert sentence**

In `security_alert_meaning.alerting_modify`, replace the text `every hockeytrack-security and scoreboard-iot alarm` with `every hockeytrack-security and scoreboard alarm`.

- [ ] **Step 4: Correct the threat model**

In `docs/threat-model.md`, replace:

```markdown
a single prefix covers all three. The scoreboard's six IoT authorization alarms
publish to the same topic but are named `scoreboard-iot-*`, so that prefix is
listed as well.
```

with:

```markdown
a single prefix covers all three. The scoreboard's alarms publish to the same
topic and are all named `scoreboard-*`, so that prefix is listed as well; the
scoreboard's own tests keep every alarm there inside it.
```

Replace:

```markdown
3. `aws cloudwatch describe-alarms` for the `hockeytrack-security` and
   `scoreboard-iot` prefixes.
```

with:

```markdown
3. `aws cloudwatch describe-alarms` for the `hockeytrack-security` and
   `scoreboard` prefixes.
```

Replace:

```markdown
4. Run a plan in this repository, and one in the scoreboard for its IoT alarms.
```

with:

```markdown
4. Run a plan in this repository, and one in the scoreboard for its alarms.
```

- [ ] **Step 5: Validate and check the pattern still fits**

Run: `cd /home/jay/projects/hockeytrack/terraform && export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime" && terraform fmt -check && terraform validate`
Expected: success. The prefix is shorter, so the pattern stays under its existing `<= 2048` precondition.

- [ ] **Step 6: Commit**

```bash
cd /home/jay/projects/hockeytrack
git add terraform/security-alarms.tf docs/threat-model.md
git commit -m "security: watch every scoreboard alarm for rewriting, not only the IoT ones

Section 9 found the scoreboard's security alarms by the prefix
scoreboard-iot-. The scoreboard has since added enrollment alarms, a
sign-in refusal alarm and two sign-in gate alarms. All publish to the
security topic, and none matched, so each could be rewritten to never
fire without a page. The prefix is now scoreboard-, which every alarm
there carries, and the scoreboard's tests enforce it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 3: The gate's own alarms (hockeytrack-scoreboard)

**Files:**
- Modify: `/home/jay/projects/hockeytrack-scoreboard/terraform/signin.tf`
- Modify: `/home/jay/projects/hockeytrack-scoreboard/terraform/iot-alarms.tf`
- Modify: `/home/jay/projects/hockeytrack-scoreboard/site/tests/signin-config.test.js`
- Modify: `/home/jay/projects/hockeytrack-scoreboard/docs/superpowers/specs/2026-09-13-google-sign-in-design.md`

**Interfaces:**
- Consumes: `aws_cloudwatch_log_group.authgate`, `aws_lambda_function.authgate` and `data.aws_sns_topic.security_alerts`, all of which exist; and Task 2's `scoreboard-` prefix convention.
- Produces: the alarms `scoreboard-authgate-failures` and `scoreboard-authgate-throttles`, which Task 4 breaks on purpose.

Work on the existing branch `signin-detection` in `/home/jay/projects/hockeytrack-scoreboard`.

- [ ] **Step 1: Write the failing tripwires**

Append to `site/tests/signin-config.test.js`:

```js
// The gate refuses by returning an error, so Lambda's Errors metric counts
// every refusal and cannot tell a crash from a stranger. The failures filter
// matches only lines the Lambda runtime writes when the function itself fails.
// These lines are what a refusal really writes, taken from the live log
// group on 2026-09-14 (address and request ID replaced). If any term of the
// filter occurs in one of them, the alarm would page on every refusal.
const refusalLines = [
  '2026/09/14 22:25:42 WARN sign-in refused trigger=PreSignUp_ExternalProvider reason="not invited" domain=example.com',
  '2026/09/14 22:25:42 {"errorMessage":"this account is not invited","errorType":"errorString"}',
  "REPORT RequestId: 00000000-0000-0000-0000-000000000000\tDuration: 1.31 ms\tBilled Duration: 2 ms\tMemory Size: 128 MB\tMax Memory Used: 41 MB",
];

function failuresTerms() {
  const filter = code(block(signin, 'resource "aws_cloudwatch_log_metric_filter" "authgate_failures" {'));
  const m = filter.match(/pattern\s*=\s*"((?:[^"\\]|\\.)*)"/);
  assert.ok(m, "authgate_failures pattern not found");
  return [...m[1].replace(/\\"/g, '"').matchAll(/\?"([^"]+)"/g)].map((t) => t[1]);
}

test("the crash filter watches the runtime's own failure lines", () => {
  const terms = failuresTerms();
  for (const want of ["Runtime.ExitError", "Status: timeout", "panic:"]) {
    assert.ok(terms.includes(want), `failures pattern lacks "${want}"`);
  }
});

test("the crash filter never matches what a refusal writes", () => {
  for (const line of refusalLines) {
    for (const term of failuresTerms()) {
      assert.ok(!line.includes(term), `"${term}" occurs in a refusal line: ${line}`);
    }
  }
});

test("the gate's crash and throttle alarms notify the security topic", () => {
  for (const name of ["authgate_failures", "authgate_throttles"]) {
    const alarm = code(block(signin, `resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    assert.match(alarm, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
  }
});

// HockeyTrack's rewrite-detection rule finds this stack's security alarms by
// the "scoreboard-" prefix. An alarm without it can be silently rewritten.
test("every alarm keeps the prefix HockeyTrack watches for rewriting", () => {
  const dir = new URL("../../terraform/", import.meta.url);
  const names = readdirSync(dir)
    .filter((f) => f.endsWith(".tf"))
    .flatMap((f) => [...code(readFileSync(new URL(f, dir), "utf8")).matchAll(/alarm_name\s*=\s*"([^"]+)"/g)].map((m) => m[1]));
  assert.ok(names.length >= 11, `found only ${names.length} alarm names`);
  for (const n of names) assert.ok(n.startsWith("scoreboard-"), `alarm "${n}" lacks the scoreboard- prefix`);
});
```

Change the file's first import block from:

```js
import { readFileSync } from "node:fs";
```

to:

```js
import { readFileSync, readdirSync } from "node:fs";
```

- [ ] **Step 2: Run them and confirm the right ones fail**

Run: `cd /home/jay/projects/hockeytrack-scoreboard/site && node --test tests/signin-config.test.js`
Expected: the first three new tests FAIL, because the resources do not exist yet. The prefix test PASSES already: all 11 existing alarms carry the prefix. That is expected, because it is a tripwire for future renames. Capture the output.

- [ ] **Step 3: Add the alarms**

Append to `terraform/signin.tf`:

```hcl

# The gate fails closed when it cannot run, which is the right failure and a
# silent one. A crash, a timeout or a throttle refuses the sign-in without
# writing "sign-in refused", so scoreboard-signin-refused never counts it, and
# the owner simply cannot get in.
#
# Lambda's Errors metric is not the answer, because every refusal is an Errors
# datapoint: the gate refuses by returning an error. Metric math subtracting
# refusals was rejected too, because a refusal's error and its log line can fall
# in adjacent five-minute periods and page on nothing. This filter matches only
# lines the Lambda runtime writes when the function itself fails. A refusal
# writes none of them: its REPORT line carries no Status field, which was
# checked against the live log group, and a test holds the pattern to that.
#
#   Runtime.ExitError, Runtime exited   the process died, including at startup
#   Status: error, Status: timeout      the platform's verdict on the invocation
#   Task timed out                      the older wording for a timeout
#   panic:                              a Go panic's own first line
#
# One failure can write several of these lines, so the metric counts lines, not
# failures. The threshold is one.
resource "aws_cloudwatch_log_metric_filter" "authgate_failures" {
  name           = "scoreboard-authgate-failures"
  log_group_name = aws_cloudwatch_log_group.authgate.name
  pattern        = "?\"Runtime.ExitError\" ?\"Runtime exited\" ?\"Status: error\" ?\"Status: timeout\" ?\"Task timed out\" ?\"panic:\""

  metric_transformation {
    name      = "AuthgateFailures"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "authgate_failures" {
  alarm_name          = "scoreboard-authgate-failures"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "AuthgateFailures"
  alarm_description   = <<-EOT
    The scoreboard sign-in gate (scoreboard-authgate) crashed or timed out. It
    fails closed, so nobody, the owner included, can sign in to the admin site
    while this lasts. Read /aws/lambda/scoreboard-authgate for the failing
    lines, and check the function's configuration against the scoreboard
    repository: an emptied ALLOWLIST_PARAMETER makes it exit at startup.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

# A throttled invocation never starts, so it writes no log line at all, and
# Lambda counts it in neither Invocations nor Errors. Only this metric sees it.
# The function has no reserved concurrency, so a throttle means the account's
# concurrency is exhausted, or someone set this function's to zero.
resource "aws_cloudwatch_metric_alarm" "authgate_throttles" {
  alarm_name          = "scoreboard-authgate-throttles"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.authgate.function_name }
  alarm_description   = <<-EOT
    The scoreboard sign-in gate (scoreboard-authgate) was throttled. It fails
    closed, so sign-in to the admin site is refused while this lasts. Check
    `aws lambda get-function-concurrency --function-name scoreboard-authgate`
    (it should have none) and the account's concurrent executions.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}
```

- [ ] **Step 4: Update the load-bearing-names comment**

In `terraform/iot-alarms.tf`, replace:

```hcl
# These alarm names are load-bearing outside this repository. HockeyTrack's
# hockeytrack-sec-alerting-modification rule pages when a security alarm is
# rewritten rather than deleted, and it finds this stack's alarms by the
# "scoreboard-iot-" prefix, because they publish to its security topic.
# Renaming them here silently drops that coverage: nothing fails, no plan
# shows a difference, and the alarms simply stop being watched. Rename them
# only alongside that rule.
```

with:

```hcl
# These alarm names are load-bearing outside this repository. HockeyTrack's
# hockeytrack-sec-alerting-modification rule pages when a security alarm is
# rewritten rather than deleted, and it finds this stack's alarms by the
# "scoreboard-" prefix, because they publish to its security topic. An alarm
# anywhere in this stack without that prefix is silently unwatched: nothing
# fails in AWS and no plan shows a difference. site/tests/signin-config.test.js
# fails instead, so keep the prefix on every alarm.
```

- [ ] **Step 5: Mark the known gaps closed**

In `docs/superpowers/specs/2026-09-13-google-sign-in-design.md`, replace:

```markdown
### 4.6 Known gaps

```

with:

```markdown
### 4.6 Known gaps

> **Both closed 2026-09-14** by `2026-09-14-signin-detection-design.md`: a
> HockeyTrack rule pages on any write naming the pool, the function or the
> invite list, and this stack alarms on the gate crashing or being throttled.
> The text below is kept as the record of what was open at deploy.

```

- [ ] **Step 6: Run the tests and validate**

Run: `cd /home/jay/projects/hockeytrack-scoreboard && make test-js && cd terraform && export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime" && terraform fmt -check && terraform validate`
Expected: every site test passes, including the four new ones; `fmt` is clean; `Success! The configuration is valid.`

- [ ] **Step 7: Mutation check (no commit)**

Add ` ?\"errorMessage\"` to the end of the failures `pattern` string, and run `cd site && node --test tests/signin-config.test.js`. Expected: `the crash filter never matches what a refusal writes` FAILS. Then run `git checkout -- terraform/signin.tf` if Step 8's commit has already happened, or undo the edit by hand otherwise. Report the outcome.

- [ ] **Step 8: Commit**

```bash
cd /home/jay/projects/hockeytrack-scoreboard
git add terraform/signin.tf terraform/iot-alarms.tf site/tests/signin-config.test.js docs/superpowers/specs/2026-09-13-google-sign-in-design.md
git commit -m "terraform: alarm when the sign-in gate crashes or is throttled

The gate fails closed when it cannot run, which refuses the sign-in
without writing the line the refusal alarm counts. The owner would be
locked out and nothing would say why.

Errors cannot tell the difference, because the gate refuses by
returning an error. So crashes come from a log filter on the lines the
Lambda runtime writes when the function itself fails: runtime exit,
platform status, timeout, panic. A test holds the filter to never
matching a real refusal's lines, taken from the live log group. A
throttled invocation writes no log, so the Throttles metric covers it.

A second test requires every alarm in this stack to carry the
scoreboard- prefix, which HockeyTrack's rewrite detection now watches.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

Run Step 7 after this commit, so `git checkout` restores the file cleanly.

---

### Task 4: Prove the rule, deploy both, break each alert (controller and user, not a subagent)

The controller does every read-only step and hands the user exact commands for every write, which the user runs with `!`. Every AWS command passes `--region us-east-1`. Nothing prints the invite list's value, the Google client secret, or an email address. Scratch files go in the session scratchpad (`<scratchpad>` below).

- [ ] **Step 1: Plan HockeyTrack and read the diff**

`cd /home/jay/projects/hockeytrack/terraform && terraform plan -input=false -out=<scratchpad>/hockeytrack-detection.tfplan`, read with `terraform show` (never `-json`).

Expected, and nothing else:
- `+` `aws_cloudwatch_event_rule.scoreboard_signin`
- `+` `aws_cloudwatch_event_target.security["scoreboard_signin"]`
- `~` `aws_sns_topic_policy.security` (one more SourceArn)
- `~` `aws_sqs_queue_policy.security_dlq` (one more SourceArn)
- `~` `aws_cloudwatch_event_rule.alerting_modification` (the prefix)

Both preconditions pass, or the plan fails.

- [ ] **Step 2: Render the pattern and prove it against real events before applying**

Render it: `cd /home/jay/projects/hockeytrack/terraform && echo 'local.scoreboard_signin_pattern' | terraform console | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()))' > <scratchpad>/signin-pattern.json`. Record its length, which must be at most 2048.

Write `<scratchpad>/proof.py`. For each CloudTrail record it wraps it in an EventBridge envelope:

```
{"version":"0","id":"<uuid4>","detail-type":"AWS API Call via CloudTrail","source":"aws.<service>","account":"<account>","time":"<eventTime>","region":"us-east-1","resources":[],"detail":<record>}
```

where `<service>` is the eventSource without `.amazonaws.com`. It then calls `aws events test-event-pattern --region us-east-1 --event-pattern file://<scratchpad>/signin-pattern.json --event file://<envelope>` and prints the eventName, the eventSource and `Result`, never the record. Feed it records from `aws cloudtrail lookup-events` (`--query 'Events[].CloudTrailEvent'`), filtered in Python to the events named below:

- **Must match:**
  - `UpdateUserPool`, `UpdateUserPoolClient`, `CreateIdentityProvider` and `AdminDeleteUser` on this pool, all from 2026-09-14.
  - `CreateFunction20150331` and `AddPermission20150331v2` for `scoreboard-authgate`.
  - `PutParameter` for `/scoreboard/allowed-emails`.
- **Must not match:**
  - `DescribeUserPool` and `ListUsers` on this pool, which are reads.
  - `Token_POST` and `OAuth2Response_GET`, which are sign-in traffic.
  - `InitiateAuth` and `SignUp` from the 2026-09-14 probes.
  - `PutParameter` for `/ebook-share/allowed-emails`, LitLibrary's list.
  - An `UpdateFunctionCode20150331v2` for `scoreboard-api`, from the 2026-09-14 apply.
  - Any write in the last 90 days to a Cognito pool other than this one, if one exists.

Then sweep: for each event source (`cognito-idp`, `lambda`, `ssm`), pull 90 days of events and count how many a local re-implementation of this pattern would match. The pattern is `readOnly` false, and any of the seven fields equal to its values, or satisfying its wildcard. Name each match by eventName and date. Every match must be a change to one of the three resources. Spot-check three of the matches and three non-matches with `test-event-pattern` so the local matcher agrees with AWS. Record the counts for the commit and PR.

Any mismatch stops the task: fix the pattern, return to Task 1's review, and replan.

- [ ] **Step 3: The user applies HockeyTrack**

Hand the user: `! cd /home/jay/projects/hockeytrack/terraform && XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime" terraform apply <scratchpad>/hockeytrack-detection.tfplan`

Then run `terraform plan -detailed-exitcode`, which must exit 0. Read the deployed rule back with `aws events describe-rule --region us-east-1 --name hockeytrack-sec-scoreboard-signin`: its pattern must equal the rendered one. Then run `list-targets-by-rule`, which should show one target, the security topic, with its DLQ.

Applying HockeyTrack's own rules writes EventBridge resources, which section 9 may page on. That is expected.

- [ ] **Step 4: Plan and apply the scoreboard alarms**

Build first: `cd /home/jay/projects/hockeytrack-scoreboard && make build`. Then `cd terraform && terraform plan -input=false -out=<scratchpad>/scoreboard-detection.tfplan`. Expected: 3 to add (the metric filter and two alarms), and no other change. The other functions' code hashes should be unchanged, because `go.mod` did not change. Hand the user the apply of the saved plan, then confirm `terraform plan -detailed-exitcode` exits 0.

- [ ] **Step 5: Break each branch of the rule**

Read the security email after each step; the user confirms it arrived. After all of them, check the security dead-letter queue: `aws sqs get-queue-attributes --region us-east-1 --queue-url "$(aws sqs get-queue-url --region us-east-1 --queue-name hockeytrack-security-alerts-dlq --query QueueUrl --output text)" --attribute-names ApproximateNumberOfMessages` must be 0.

1. **SSM `name`.** Save the list privately:

   ```
   aws ssm get-parameter … --query Parameter.Value --output text | tr -d '\n' > <scratchpad>/list.txt; chmod 600
   ```

   Hand the user:

   ```
   ! aws ssm put-parameter --region us-east-1 --name /scoreboard/allowed-emails --type String --overwrite --value "file://<scratchpad>/list.txt"
   ```

   Expected: an email naming `PutParameter`. Compare the value afterwards without printing it, then delete the file.
2. **Lambda `functionName`.** Hand the user:

   ```
   ! aws lambda update-function-configuration --region us-east-1 --function-name scoreboard-authgate --timeout 5
   ```

   Expected: an email naming `UpdateFunctionConfiguration20150331v2`.
3. **Lambda `resource`.** Hand the user:

   ```
   ! aws lambda tag-resource --region us-east-1 --resource <function ARN> --tags detection-probe=1
   ```

   then

   ```
   ! aws lambda untag-resource --region us-east-1 --resource <function ARN> --tag-keys detection-probe
   ```

   Expected: two emails.
4. **Cognito `userPoolId`.** Hand the user:

   ```
   ! aws cognito-idp create-group --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --group-name detection-probe
   ```

   then

   ```
   ! aws cognito-idp delete-group --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --group-name detection-probe
   ```

   Expected: two emails.
5. **Negatives.** Run a scoreboard `terraform plan`, and the owner signs in once. Expected: no email from this rule.

If an email does not arrive within 15 minutes, find out why before going on. CloudTrail delivery to EventBridge usually takes under five minutes. Read `aws cloudwatch get-metric-statistics --namespace AWS/Events --metric-name MatchedEvents --dimensions Name=RuleName,Value=hockeytrack-sec-scoreboard-signin …`, and the `FailedInvocations` metric, before touching the pattern.

- [ ] **Step 6: Break the gate's alarms**

1. **Crash.** Save the function's current environment privately:

   ```
   aws lambda get-function-configuration --region us-east-1 --function-name scoreboard-authgate --query Environment > <scratchpad>/env.json
   ```

   It holds only the parameter name, but keep it private anyway. Hand the user:

   ```
   ! aws lambda update-function-configuration --region us-east-1 --function-name scoreboard-authgate --environment 'Variables={ALLOWLIST_PARAMETER=}'
   ```

   Wait until `get-function-configuration --query LastUpdateStatus` reads `Successful`. The owner attempts one sign-in: refused. Expected:
   - `/aws/lambda/scoreboard-authgate` shows the runtime exit lines;
   - `scoreboard-authgate-failures` goes to `ALARM`;
   - the email arrives.

   Restore:

   ```
   ! aws lambda update-function-configuration --region us-east-1 --function-name scoreboard-authgate --environment file://<scratchpad>/env.json
   ```

   The owner signs in again: admitted. Record the exact failure lines the runtime wrote. This is the evidence for the filter's terms. Delete `env.json`.
2. **Throttle.** Hand the user:

   ```
   ! aws lambda put-function-concurrency --region us-east-1 --function-name scoreboard-authgate --reserved-concurrent-executions 0
   ```

   The owner attempts one sign-in: refused. Expected: `scoreboard-authgate-throttles` goes to `ALARM`, and the email arrives. Restore:

   ```
   ! aws lambda delete-function-concurrency --region us-east-1 --function-name scoreboard-authgate
   ```

   The owner signs in again: admitted.
3. **Rewrite coverage.** Read `scoreboard-signin-refused`'s definition with `describe-alarms`. Hand the user a `put-metric-alarm` that re-saves exactly those values. Expected: the alerting-modification email, naming `PutMetricAlarm` and this alarm.
4. **Drift.** Run `terraform plan -detailed-exitcode` in both repositories. Both must exit 0.

Both the crash and throttle breaks also fire Step 5's rule, because they are Lambda writes. That is expected.

The timeout line (`Status: timeout` / `Task timed out`) cannot be induced safely. Confirm its wording against AWS's Lambda documentation for text-format logs, and record that this term is documented, not observed.

- [ ] **Step 7: Record and hand off**

Append `## 7. Verification record (2026-09-14)` to `docs/superpowers/specs/2026-09-14-signin-detection-design.md`. Include:
- the pattern's length;
- every must-match and must-not-match result;
- the 90-day sweep counts, per source, with what matched;
- each break and the email it produced;
- the exact runtime failure lines from the crash;
- the throttle result;
- the drift checks;
- that the timeout term is documented rather than observed.

Commit it in hockeytrack-scoreboard: `docs: record the sign-in detection proof`. Then offer the user the finishing choice for both branches: two pull requests, one per repository, each description linking the other.
