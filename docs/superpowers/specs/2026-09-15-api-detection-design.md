# Detection for the Admin API's Authorization Roots — Design

**Status:** accepted (design approved in conversation, 2026-09-15)
**Date:** 2026-09-15
**Closes:** the gap named in `2026-09-14-signin-detection-design.md` §6: the admin API's JWT authorizer, routes, integrations and stage, and the `scoreboard-api` and `scoreboard-enroll` functions.
**Repositories:** both. The rule goes in HockeyTrack (`terraform/security-alarms.tf`, section 11); the build fix goes here (`Makefile`). Two pull requests.

## 1. Purpose

The sign-in gate decides who gets a token. The admin API decides what a token is worth. `cloud/cmd/api/handler.go` and `cloud/cmd/enroll/handler.go` take identity entirely from the claims API Gateway's JWT authorizer passes them (`sub`, `email`, `email_verified`), and the authorizer's issuer and audience (`terraform/admin.tf`) decide which tokens count. Whoever can change the API or those two functions can grant panel control without going near the gate. Today nothing pages when they do.

## 2. Routes that bypass the gate

| Route | Effect | CloudTrail write |
|---|---|---|
| Point the authorizer at an issuer the attacker runs | Mint a token for any `sub`, or for the owner's verified email | `UpdateAuthorizer`, `CreateAuthorizer` |
| Move a route to another authorizer, or to `NONE` with an integration that trusts a header | Claims the handler never checks, or never gets | `UpdateRoute`, `CreateRoute` |
| Repoint an integration at a function the attacker controls | Any response for any caller | `UpdateIntegration`, `CreateIntegration` |
| Change the API's or enroll function's code, configuration or role | The handler itself decides differently | `UpdateFunctionCode*`, `UpdateFunctionConfiguration*` |
| Grant another principal permission to invoke them | A second caller path | `AddPermission*` |
| Change the stage, CORS, or map a custom domain | Traffic reshaped or rerouted | `UpdateStage`, `UpdateApi`, `CreateApiMapping`, … |
| Delete the API or remove a function's permission | The admin site stops working, which fails closed | `DeleteApi`, `RemovePermission*`, … |

Removing a route's authorizer alone does not grant identity: the handler returns 401 without a `sub` (`api/handler.go`). That is why the table names routes that pair a change with something else.

## 3. Facts established by investigation (2026-09-15, read-only)

1. **Every API Gateway v2 configuration write names the API in `apiId`.** In 90 days of history, `CreateRoute`, `CreateIntegration`, `DeleteRoute`, `UpdateApi`, `CreateAuthorizer`, `CreateStage`, `DeleteIntegration`, `UpdateRoute`, `DeleteCorsConfiguration` and `UpdateStage` all carry it.
2. **Tagging names it by ARN, in a hyphenated field.** `TagResource` carries `requestParameters["resource-arn"]` = `arn:aws:apigateway:us-east-1::/apis/<id>`. A stage or route under the API extends that ARN, so a prefix match covers both.
3. **The admin API is `dk3k7p41e2`, named `scoreboard-admin`.** Three other HTTP APIs in the account belong to other projects: `davidjdrake-api`, `healthtracker-api` and `HttpApi`. A service-wide rule would page on their work.
4. **Reads do not page.** The 90 days held 1,427 API Gateway events, and 1,340 of them were reads (683 `GetRoute` alone), all `readOnly: true`.
5. **Legitimate writes to the two functions are rare.** In 90 days, `scoreboard-api` had 5 writes and `scoreboard-enroll` had 4, all from the scoreboard's own applies. Most of the code updates changed no code (fact 6).
6. **Every commit currently redeploys every function.** Go stamps `vcs.revision` and `vcs.time` into each binary (verified 2026-09-15 by diffing `go version -m` on the deployed and a fresh `scoreboard-authgate`), so an unchanged function gets a new code hash on every commit, and every apply redeploys it. With this rule and section 10 in place, each such apply would page three times for nothing.

## 4. Design

### 4.1 The rule: `hockeytrack-sec-scoreboard-api` (HockeyTrack section 11)

A separate rule rather than more branches on section 10, so the alert names the right thing to check: the authorizer, routes and integrations, not the invite list. It is registered in `security_rules` like every other rule, which gives it the SNS target, dead-letter queue, topic and queue policies, and its own alert sentence.

```hcl
event_pattern = jsonencode({
  "detail-type" = ["AWS API Call via CloudTrail"]
  "detail" = {
    "eventSource" = ["apigateway.amazonaws.com", "lambda.amazonaws.com"]
    "readOnly"    = [false]
    "$or" = [
      { "requestParameters" = { "apiId"        = local.scoreboard_api_ids } },
      { "requestParameters" = { "resource-arn" = [for id in local.scoreboard_api_ids : { "prefix" = "arn:aws:apigateway:${var.region}::/apis/${id}" }] } },
      { "requestParameters" = { "functionName" = [{ "wildcard" = "*scoreboard-api*" }, { "wildcard" = "*scoreboard-enroll*" }] } },
      { "requestParameters" = { "resource"     = [{ "wildcard" = "*:function:scoreboard-api*" }, { "wildcard" = "*:function:scoreboard-enroll*" }] } },
    ]
  }
})
```

- **The API ID is looked up by name.** `data "aws_apigatewayv2_apis"` looks up `scoreboard-admin`, with a precondition that exactly one matches. A `for` expression over the looked-up IDs, rather than `one()`, lets zero or several matches reach the precondition's readable message instead of failing inside the pattern. A replaced API is picked up at HockeyTrack's next apply, the same window section 10 already names for the pool.
- **No event names, scoped by resource, over-matching wildcards.** The reasoning is section 10's. Implementation confirms the complete field list from the apigatewayv2 and lambda service models; §3 is what the observed events show.
- **Length precondition** `<= 2048`, the same as sections 9 and 10.

Alert sentence: *If this was not you, assume the scoreboard admin API may accept tokens or requests it should not. Check its JWT authorizer's issuer and audience, its routes' authorizers and integrations, and the scoreboard-api and scoreboard-enroll functions' code, configuration and permissions, against the scoreboard repository.*

**Accepted noise:** scoreboard applies that change the API or either function, which is rare and deliberate. It is only rare once 4.2 lands.

### 4.2 Stop redeploying unchanged functions (this repository)

`make build` passes `-buildvcs=false` and `-trimpath` to every `go build`: Go otherwise stamps the commit and the checkout's absolute path into each binary. An unchanged function then builds to the same bytes at any commit, so `archive_file`'s hash, and with it the apply and the page, only moves when code moves. Terraform's archive_file also pins `output_file_mode = "0755"`, so the zip does not inherit the build host's umask. Proven in implementation by building at two different commits and comparing hashes.

### 4.3 The threat model and the sign-in spec

- **HockeyTrack's threat model:**
  - §4 gains a paragraph for the rule.
  - §7 gains a recovery entry: compare the authorizer's issuer and audience, list routes and their authorizers and integrations, and check both functions' code SHA, role and resource policy against the scoreboard repository.
  - Section 10's and §4's text naming the API as unwatched is updated to point at section 11.
- **The sign-in detection spec's §6:** the admin API gap is marked closed by this spec.

## 5. Testing

- **Before applying:** `test-event-pattern` on real events.
  - *Must match:* `CreateRoute`, `CreateAuthorizer`, `CreateIntegration` and `UpdateStage` on `dk3k7p41e2`; `CreateFunction20150331` and `AddPermission20150331v2` for both functions.
  - *Must not match:* reads on `dk3k7p41e2`; `CreateRoute` and `TagResource` on `op8gfgqr8f` and `bjmqkenm95`; `UpdateFunctionCode20150331v2` on `scoreboard-authgate` and on HealthTracker's functions.
  - *Then:* a 90-day sweep with a local matcher, spot-checked against `test-event-pattern`, including the check for events with no `readOnly` key.
- **Build:** the same function built at two commits yields identical `archive_file` hashes, and a scoreboard plan after a docs-only commit shows no Lambda changes.
- **Live breaks,** each harmless and each expected to email:
  - `tag-resource` / `untag-resource` on the API's ARN (`resource-arn`);
  - `update-stage` re-saving the stage's current throttle settings (`apiId`);
  - `tag-resource` / `untag-resource` on each function (`resource`);
  - `update-function-configuration` re-saving each function's current timeout (`functionName`).
- **Negatives:** a scoreboard plan and normal admin-site use (sign-in, listing panels) produce no email.
- **After the breaks:** drift checks in both repositories, and a verification record in this spec.

## 6. Out of scope

- **The api and enroll roles' IAM policies.** HockeyTrack's identity rule deliberately excludes role-policy churn, but `scoreboard-enroll`'s role can mint device certificates, so a widened grant there is a real, unwatched route. Named here and in section 11's "does not see".
- **The devices table's ownership rows.** Writes to them are DynamoDB data events, which the trail does not log.
- **The static site** (S3 and CloudFront), which could serve a look-alike page from the real domain. It remains its own gap.
- **The IoT publish path** that the API uses to push configuration, which sections 7 and 8 already cover.
