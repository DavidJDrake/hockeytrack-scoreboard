# Closing Direct Invocation of the Admin Functions — Design

**Status:** accepted (design approved in conversation, 2026-09-15)
**Date:** 2026-09-15
**Closes:** the first gap named in `2026-09-15-api-detection-design.md` §6: invoking `scoreboard-api` or `scoreboard-enroll` with a hand-built event carrying forged claims, skipping API Gateway and its authorizer.
**Repositories:** both. Token verification and its alarm go here; the trail change and the rule go in HockeyTrack (`terraform/cloudtrail.tf`, `terraform/security-alarms.tf` section 12, `docs/threat-model.md`). Two branches: `direct-invoke` here, `scoreboard-invoke-detection` in HockeyTrack.

## 1. Purpose

API Gateway's JWT authorizer checks the caller's token, then hands the function the token's claims inside the event. The functions trust those claims completely: `cloud/cmd/api/handler.go` takes `sub` from `requestContext.authorizer.jwt.claims`, and `cloud/cmd/enroll/handler.go` takes `sub`, `email`, `email_verified` and `cognito:username` from the same place on `POST /api/devices/claim`. The event is only as trustworthy as whoever sent it. Anyone who can call `lambda:Invoke` on either function directly can write any claims they like and act as any owner: list and reconfigure their panels, publish to their devices, claim an enrollment. No configuration changes, so no existing rule sees it, and the account's trail does not log Invoke.

This design does two independent things:

- **Prevent:** both functions verify the raw ID token themselves and ignore the authorizer's claims. A forged event is refused whoever sends it.
- **Detect:** the trail logs Invoke for the three admin-path functions, and a HockeyTrack rule pages on any invocation not made by the service each function exists to serve. That also covers what verification cannot: `scoreboard-authgate`, whose events carry no token, and a genuine token replayed through a direct invoke.

## 2. Facts established by investigation (2026-09-15, read-only)

1. **Only the intended services are granted invoke by resource policy.** `scoreboard-api` and `scoreboard-enroll` allow `apigateway.amazonaws.com` with `AWS:SourceArn` `arn:aws:execute-api:us-east-1:989232581535:dk3k7p41e2/*/*`. `scoreboard-authgate` allows `cognito-idp.amazonaws.com` with the pool's ARN. A direct invoke therefore needs an identity policy granting `lambda:InvokeFunction`, which in this account means `funandgames` (AdministratorAccess), `healthtracker-deploy` (AdministratorAccess), or a role that can reach one of them. Adding a resource-policy grant is an `AddPermission` that section 11 already pages on.
2. **`hockeytrack-foreign-project-deny` does not cover Lambda.** It denies the raw archive, trail tampering and alarm silencing, and is attached to `davidjdrake` and `healthtracker-deploy` only.
3. **The trail logs no Lambda data events.** `hockeytrack-account` has one basic event selector: management events plus write-only S3 object events on `hockeytrack-raw-989232581535/`.
4. **The site sends the ID token.** `site/assets/api.js` sets `authorization: Bearer <token>` from `session.idToken`. The authorizer (`terraform/admin.tf`) reads `$request.header.Authorization`, with `issuer = https://<pool endpoint>` and `audience = [<site client id>]`.
5. **Only one enroll route uses identity.** `POST /api/enroll` and `GET /api/enroll` have `authorization_type = "NONE"` and authenticate a device's collection token, which is not a JWT. `POST /api/devices/claim` is JWT-authorized.
6. **Records carry `eventCategory`.** This account's CloudTrail records are version 1.11 and carry `"eventCategory": "Management"`, so a rule can exclude data events by category.
7. **The other two scoreboard functions have different invokers.** `scoreboard-reducer` is invoked by EventBridge rule `hockeytrack/scoreboard-game-events`, on every game event. `scoreboard-today` has no resource policy; EventBridge Scheduler invokes it through the `scoreboard-today` role (`terraform/scheduler.tf`, `terraform/iam.tf`).

## 3. Design: verification in the functions (this repository)

### 3.1 `internal/idtoken`

A new package at `cloud/internal/idtoken`, beside `devices` and `enroll`.

```go
type Claims struct {
    Sub             string
    Email           string
    EmailVerified   bool
    CognitoUsername string
}

type Verifier struct { /* issuer, audience, key cache, clock, HTTP client */ }

func New(region, userPoolID, clientID string) *Verifier
func (v *Verifier) Verify(ctx context.Context, authorizationHeader string) (Claims, error)
```

- **Parsing and claim checks use `github.com/golang-jwt/jwt/v5`**, pinned in `go.mod` and `go.sum`. It has no dependencies of its own. No JWKS library is added.
- **A token is accepted only if every check passes:**
  - the header is `Bearer <token>`, case-insensitive on the scheme, or a bare token, matching what API Gateway accepts;
  - `alg` is exactly `RS256` (`jwt.WithValidMethods([]string{"RS256"})`), which refuses `none` and HMAC tokens signed with the public key;
  - `kid` names a key in the pool's JWKS and the RSA signature verifies against it;
  - `iss` is `https://cognito-idp.<region>.amazonaws.com/<userPoolID>`;
  - `aud` is exactly the site client ID;
  - `token_use` is `"id"`;
  - `exp` is present and in the future, with at most 30 seconds of leeway.
- **`email_verified` is accepted as JSON `true` or the string `"true"`.** Cognito writes booleans for native attributes but federated mappings can arrive as strings. Anything else is false.
- **The JWKS** comes from `https://cognito-idp.<region>.amazonaws.com/<userPoolID>/.well-known/jwks.json`.
  - It is fetched on first use, not at init, so a Cognito blip does not crash-loop the function, and cached for the life of the execution environment.
  - An unknown `kid` triggers one refetch, at most once every 5 minutes. A stream of junk `kid`s costs Cognito one request per 5 minutes per environment.
  - Fetches use a 5-second timeout and read at most 64 KiB.
  - Keys that are not `kty: RSA` with `use: sig` (or no `use`) are ignored.
- **Errors are typed.** `ErrUnavailable` wraps a failed key fetch; every other failure wraps `ErrInvalid`. Messages name which check failed, never the token or its claims.

### 3.2 The handlers

- `Handler` in both commands gains a `Tokens` field of an interface type with the `Verify` method, so tests inject a verifier built from a test key.
- `main.go` in both reads `USER_POOL_ID` and `APP_CLIENT_ID`, exits with an error like the existing variables if either is empty, and builds the verifier with the region from `AWS_REGION`, which Lambda sets.
- `subject()` in `api` and `enroll` is removed. Every route in `api`, and `POST /api/devices/claim` in `enroll`, calls `Verify` on the `Authorization` header first. The owner-hint check in `enroll` takes `cognito:username`, `email` and `email_verified` from the returned `Claims`. `requestContext.authorizer` is never read for identity again.
- **`ErrInvalid` returns 401** `{"error":"unauthenticated"}`, the body the handlers return today without a `sub`.
- **`ErrUnavailable` returns 503** `{"error":"sign-in check unavailable"}` and logs at error level. It fails closed, but the caller is not blamed.
- `enroll`'s own `Authorization` header use on the two unauthenticated routes is unchanged.

### 3.3 The mismatch signal

A request where API Gateway's authorizer accepted a token and the function rejected it should never happen on the real path: both check the same token against the same issuer and audience. It is the fingerprint of a hand-built event carrying authorizer claims.

- When `requestContext.authorizer.jwt` is present and `Verify` returns `ErrInvalid`, the handler logs at warn level with the fixed message `token rejected after authorizer accepted`, plus the failed check and the route key. The token and claims are never logged.
- **In `terraform/admin.tf`:**
  - A metric filter on each of `/aws/lambda/scoreboard-api` and `/aws/lambda/scoreboard-enroll` matches `"token rejected after authorizer accepted"`. Both publish `TokenMismatch` in namespace `Scoreboard`.
  - One alarm, `scoreboard-token-mismatch`, fires on sum ≥ 1 in 300 seconds, with `treat_missing_data = "notBreaching"`. It sends to the same `data.aws_sns_topic.security_alerts` as the gate alarms.
- This does not depend on the trail at all, so it is a second, independent path to a page if section 12 is ever broken.

### 3.4 Terraform

- `aws_lambda_function.api` and `aws_lambda_function.enroll` gain `USER_POOL_ID = aws_cognito_user_pool.admin.id` and `APP_CLIENT_ID = aws_cognito_user_pool_client.site.id`.
- No IAM change: the JWKS is a public, unauthenticated URL, and both functions already reach the internet.

## 4. Design: detection (HockeyTrack)

### 4.1 The trail logs the three functions' invocations

`aws_cloudtrail.account` gains a second, unconditional `event_selector`:

```hcl
event_selector {
  read_write_type           = "All"
  include_management_events = false
  data_resource {
    type   = "AWS::Lambda::Function"
    values = [for f in data.aws_lambda_function.scoreboard_admin_path : f.arn]
  }
}
```

- The functions are found by name through `data "aws_lambda_function"` over `scoreboard-api`, `scoreboard-enroll` and `scoreboard-authgate`. A missing function fails the plan instead of logging nothing.
- `read_write_type = "All"` removes any dependence on how Lambda classifies Invoke. Lambda's data events are invocations only, so there is no read volume to exclude.
- `include_management_events = false` on this selector only. The existing selector keeps management events on, and CloudTrail does not double-log.
- **Cost:** CloudTrail charges $0.10 per 100,000 data events, plus CloudWatch Logs ingestion into the trail's log group. Admin-site use is at most a few thousand invocations a month, well under $0.01.
- **Expected page:** the apply is a `PutEventSelectors` on the trail, which `hockeytrack-sec-audit-tampering` pages on. That page is the proof the rule still works, not noise.

### 4.2 Sections 10 and 11 ignore data events

Both rules match `requestParameters.functionName` on these functions. Once Invoke is logged, every sign-in (section 10, `scoreboard-authgate`) and every admin API request (section 11) would page.

- Each rule's `detail` gains `"eventCategory" = ["Management"]`.
- Section 10's comment stops saying "if any trail in this account ever logged Lambda data events" and says that the trail does, for these functions, and that this key is why the rule ignores them.
- Section 11's "does not see" entry for direct invocation is replaced by a pointer to section 12 and to the function-side verification.
- Both changes ship in the same apply as the trail selector, so there is no window where Invoke is logged and these rules still match it.

### 4.3 Section 12: `hockeytrack-sec-scoreboard-invoke`

A new rule, registered in `local.security_rules` and `local.security_alert_meaning`. That gives it the SNS target, DLQ, topic and queue policies, and its own alert sentence.

The shape is below. The exact field that names the function (`requestParameters.functionName` as a name or an ARN, or `resources[].ARN`) and the exact caller fields are written from real records captured in §5 step 2, not from this sketch.

```hcl
event_pattern = jsonencode({
  "detail-type" = ["AWS API Call via CloudTrail"]
  "detail" = {
    "eventSource"   = ["lambda.amazonaws.com"]
    "eventCategory" = ["Data"]
    "$or" = [
      { "requestParameters" = { "functionName" = local.scoreboard_api_path_functions }, "userIdentity" = { "invokedBy" = [{ "anything-but" = ["apigateway.amazonaws.com"] }] } },
      { "requestParameters" = { "functionName" = local.scoreboard_api_path_functions }, "userIdentity" = { "invokedBy" = [{ "exists" = false }] } },
      { "requestParameters" = { "functionName" = local.scoreboard_gate_functions },     "userIdentity" = { "invokedBy" = [{ "anything-but" = ["cognito-idp.amazonaws.com"] }] } },
      { "requestParameters" = { "functionName" = local.scoreboard_gate_functions },     "userIdentity" = { "invokedBy" = [{ "exists" = false }] } },
    ]
  }
})
```

- **Why each pair has an `exists: false` branch:** a direct invoke by an IAM user or role carries no `invokedBy` at all, and `anything-but` does not match a missing field.
- **Why no event names:** so `Invoke`, asynchronous invokes and `InvokeWithResponseStream` are covered alike.
- **Match on exact function names or ARNs,** not wildcards. The function list is fixed, and a wildcard such as `*scoreboard-api*` would also match a future `scoreboard-api-v2`.
- **Preconditions:** the pattern stays `<= 2048` characters, the same as sections 9 to 11, and each function lookup is exactly one function.

Alert sentence: *If this was not you, assume someone with credentials in this account called a scoreboard admin function directly, skipping API Gateway or Cognito. Find the caller and access key in the CloudTrail record, check what the function did in its logs at that time, revoke the key, then check the admin API and sign-in gate against the scoreboard repository.*

### 4.4 The threat model

- **§4** gains a paragraph covering both halves: why the functions verify the token themselves, and what section 12 watches. The existing section 11 paragraph stops naming direct invocation as unwatched.
- **§7** gains a recovery entry, "A scoreboard direct-invoke alert you cannot account for.", in this order:
  1. From the alert's CloudTrail record, note `userIdentity` (ARN, access key ID, session issuer), `sourceIPAddress`, the function and the time.
  2. Deactivate that access key, or revoke the role's sessions, before investigating further.
  3. Read the function's log lines from one minute before to five minutes after. A `token rejected after authorizer accepted` line means verification refused a forged event. A normal request line means a genuine token was used, and that user's sessions should be revoked as well (`admin-user-global-sign-out`).
  4. For `scoreboard-enroll`, list certificates created in that window and revoke any not accounted for, as the existing admin API entry describes.
  5. Run the existing admin API recovery entry, and the sign-in entry for an authgate alert.

## 5. Rollout and proof

The order matters. Verification comes first because it is the step that could lock the owner out of the admin site, so its proof comes before anything rests on it. The trail comes before the rule because a data-event record cannot be seen until the trail logs one.

Every apply is planned to a saved file and run by the user with `!`. No agent or subagent applies, pushes or runs mutating AWS calls.

1. **Scoreboard apply: §3.**
   - **Plan check:** only `scoreboard-api`, `scoreboard-enroll`, the two metric filters and the alarm change. Reproducible builds should keep every other function untouched.
   - **Proof:**
     - The user signs in and loads the panel list.
     - A direct `aws lambda invoke` of `scoreboard-api` with an event carrying forged authorizer claims and a junk `Authorization` header returns 401.
     - `scoreboard-token-mismatch` goes to ALARM and emails.
     - The same event with no authorizer block and no header returns 401 and does not log a mismatch.
   - **Rollback:** re-apply `main`.
2. **HockeyTrack apply A: §4.1 and §4.2.**
   - **Plan check:** only the trail's selectors and the event patterns of sections 10 and 11 change.
   - **Proof:**
     - The audit-tampering page arrives for the `PutEventSelectors`.
     - The user signs in and loads the panel list.
     - One direct invoke of `scoreboard-api` with an empty event returns 401.
     - Records for all three functions are read from `/aws/cloudtrail/hockeytrack-account`.
     - The `MatchedEvents` metrics of sections 10 and 11 stay at zero for the window.
   - **These assumptions are settled here and recorded before step 3:**
     - **A1:** how API Gateway's and Cognito's invocations identify their caller (`userIdentity.type`, `invokedBy`).
     - **A2:** which field names the function, and in what form.
   - **A3 is settled in step 3:** EventBridge receives Lambda data events from this trail. A record in the log group does not prove EventBridge delivery, and section 12 is the first rule that could show it.
   - **If A3 fails in step 3,** stop and return to the design. It fails if `test-event-pattern` matches the captured break records but the rule's `MatchedEvents` stays at zero after the live breaks. The fallback is a metric filter and alarm on the trail's log group. The unused rule is harmless in the meantime.
3. **HockeyTrack apply B: §4.3 and §4.4.**
   - **Before applying:** `test-event-pattern` on the captured records. Sign-in invocations of authgate by Cognito and API invocations by API Gateway must not match. The direct invokes must match.
   - **Breaks, each expected to email once with the section 12 sentence:** direct invokes, as `funandgames` with harmless events, of:
     - `scoreboard-api`: an empty event, refused with 401;
     - `scoreboard-enroll`: `GET /api/enroll` with no collection token, refused;
     - `scoreboard-authgate`: a pre sign-up event for an address not on the invite list, refused. The refusal adds one to `scoreboard-signin-refused`'s count, which pages only at three in an hour, so it is not expected to email.
   - **Negatives:** a real sign-in and panel list produce no section 10, 11 or 12 email.
   - **After:** DLQ depth 0, the recovery entry run read-only against the break records, and drift checks in both repositories.

## 6. Testing (this repository)

- **`internal/idtoken`,** with a test RSA key and a JWKS served by `httptest`:
  - **Accepted:** a valid token, both as `Bearer <token>` and bare.
  - **Refused:**
    - `alg: none`;
    - HS256 signed with the public key's bytes;
    - an unknown `kid`, after the one refetch;
    - a wrong `iss`, `aud` or `token_use`;
    - missing `exp`, and `exp` 31 seconds in the past;
    - a bad signature;
    - an empty header.
  - **Edge cases:**
    - `exp` 29 seconds in the past is accepted;
    - `email_verified` as `true` and as `"true"` is true, and anything else is false;
    - a second unknown `kid` within 5 minutes does not refetch;
    - a JWKS fetch failure returns `ErrUnavailable`.
- **Handlers:**
  - Each `api` route and `POST /api/devices/claim` refuses an event that has authorizer claims but no valid token, and logs the mismatch line.
  - A valid token with no authorizer block is accepted, since section 12 covers that path.
  - A 503 is returned on `ErrUnavailable`.
  - The owner-hint tests in `enroll` run on `Claims`.
- **Terraform tripwires** (`site/tests/signin-config.test.js` or a new `invoke-config.test.js`):
  - Both functions set `USER_POOL_ID` and `APP_CLIENT_ID`.
  - The metric filter's quoted term appears verbatim in both handlers' source.
  - `scoreboard-token-mismatch` is counted in the alarm-count test.
  - No handler source outside tests reads `Authorizer.JWT.Claims`.

## 7. Out of scope

- **`scoreboard-reducer`.** EventBridge invokes it on every game event, so logging it multiplies data-event volume. A forged invocation corrupts displayed game state but grants no control of a panel or a certificate. Named as a gap in section 12's "does not see".
- **`scoreboard-today`.** EventBridge Scheduler invokes it through its own role, and a direct invoke only republishes today's schedule. Named as a gap.
- **A genuine token stolen from a signed-in user and used through the API.** Verification accepts it by design, and section 12 sees it only if it is used through a direct invoke.
- **A Lambda-invoke deny in `hockeytrack-foreign-project-deny`.** It would not bind `funandgames`, and any administrator can detach it.
- **Still open from earlier specs:**
  - the functions' IAM roles;
  - the API's and functions' log groups;
  - the devices table's data plane;
  - the static site;
  - SCO-27.
