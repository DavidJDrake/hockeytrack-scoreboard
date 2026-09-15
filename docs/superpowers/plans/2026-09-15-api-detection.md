# Detection for the Admin API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Page someone whenever the scoreboard admin API or the two functions behind it are changed, and stop unchanged functions from redeploying on every commit.

**Architecture:** A new EventBridge rule, section 11 of HockeyTrack's `security-alarms.tf`, matches any CloudTrail write that names the `scoreboard-admin` API (by `apiId` or ARN) or the `scoreboard-api`/`scoreboard-enroll` functions. The scoreboard's `make build` becomes reproducible, with `-buildvcs=false` and `-trimpath`, so an apply pages only when code changes.

**Tech Stack:** Terraform (AWS provider 5.100) in two repositories; EventBridge event patterns; GNU make; `node --test` tripwires.

**Spec:** `docs/superpowers/specs/2026-09-15-api-detection-design.md` (in hockeytrack-scoreboard)

## Global Constraints

- **Repositories:** `/home/jay/projects/hockeytrack` on branch `scoreboard-api-detection`, and `/home/jay/projects/hockeytrack-scoreboard` on branch `api-detection`. Both are public. Never work on `main`.
- **Never commit, edit or delete** `terraform/terraform.tfvars` in either repository. Never commit a credential or a real email address.
- **Subagents run only `terraform fmt`, `terraform validate` and local tests.** Never `terraform plan`, `apply`, `init` or `console`, any AWS CLI command, `make deploy`, `make site`, or `git push`.
- US spelling.
- **Names:** the rule is `hockeytrack-sec-scoreboard-api`, with registry key `scoreboard_api`. The API is looked up by the name `scoreboard-admin`. The functions are `scoreboard-api` and `scoreboard-enroll`, and the region is us-east-1.
- **The rule has no `eventName` constraint.** It follows section 10's shape: scoped by resource, a `<= 2048` length precondition, and an exactly-one precondition on the lookup.
- **Commit trailers:**
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i
  ```
- **Before running Terraform,** `export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime"`. `terraform/.terraform` is already initialized in both repositories.

---

## File Structure

| Repository | File | Change |
|---|---|---|
| hockeytrack | `terraform/security-alarms.tf` | New section 11; registry entry and alert sentence; section 10's "does not see" now points at section 11 |
| hockeytrack | `docs/threat-model.md` | §4: the sign-in paragraph's API sentence, plus a new API paragraph; §7: a new recovery entry, and the sign-in entry's step 8 |
| hockeytrack-scoreboard | `Makefile` | `-buildvcs=false -trimpath` on every Lambda build |
| hockeytrack-scoreboard | `site/tests/build-config.test.js` (new) | A tripwire that every Lambda build keeps both flags |
| hockeytrack-scoreboard | `docs/superpowers/specs/2026-09-14-signin-detection-design.md` | §6: the admin API gap marked closed |
| hockeytrack-scoreboard | `docs/superpowers/specs/2026-09-15-api-detection-design.md` | §4.1 and §4.2 reconciled with the plan (see Task 2) |

---

### Task 1: The rule (hockeytrack)

**Files:**
- Modify: `/home/jay/projects/hockeytrack/terraform/security-alarms.tf`
- Modify: `/home/jay/projects/hockeytrack/docs/threat-model.md`

**Interfaces:**
- Consumes: `var.region`, `local.security_rules`, `local.security_alert_meaning`.
- Produces: `aws_cloudwatch_event_rule.scoreboard_api` (named `hockeytrack-sec-scoreboard-api`), `local.scoreboard_api_pattern` and `data.aws_apigatewayv2_apis.scoreboard_admin`. Task 3 renders and tests the pattern.

Before editing: `cd /home/jay/projects/hockeytrack && git checkout main && git pull --ff-only && git checkout -b scoreboard-api-detection`. Expected: a clean tree, with `main` at `0abbd71` or later.

- [ ] **Step 1: Register the rule and its alert sentence**

In `terraform/security-alarms.tf`, replace:

```hcl
    scoreboard_signin = aws_cloudwatch_event_rule.scoreboard_signin
  }
```

with:

```hcl
    scoreboard_signin = aws_cloudwatch_event_rule.scoreboard_signin
    scoreboard_api    = aws_cloudwatch_event_rule.scoreboard_api
  }
```

In `security_alert_meaning`, directly after the line beginning `    scoreboard_signin = "If this was not you, assume the scoreboard admin site's sign-in gate`, add:

```hcl
    scoreboard_api    = "If this was not you, assume the scoreboard admin API may accept tokens or requests it should not. Check its JWT authorizer's issuer and audience, its routes' authorizers and integrations, and the scoreboard-api and scoreboard-enroll functions' code, configuration, role and permissions, against the scoreboard repository."
```

Run `terraform fmt`.

- [ ] **Step 2: Point section 10's "does not see" at section 11**

Replace this block, exactly:

```hcl
# What it does not see, the most important first. The gate decides who gets a
# token; it does not decide what a token is worth. The scoreboard's admin API
# does, and it is a second authorization root that nothing watches.
# scoreboard-api and scoreboard-enroll take the caller's identity entirely from
# the claims API Gateway's JWT authorizer hands them -- sub, and for claiming a
# panel cognito:username, email and email_verified -- and that authorizer's
# issuer and audience decide whose tokens are believed. An UpdateAuthorizer
# naming an issuer the attacker runs, an UpdateRoute that moves a route to an
# authorizer of their own, an UpdateIntegration that hands a route to other
# code, or new code in either function, changes whose identity the API believes
# or what it does with it -- enough to claim or control panels -- without one
# write to the pool, the gate or the invite list. No rule in either
# repository watches apigateway.amazonaws.com or those two functions. The fix
# has this rule's shape: scoped to the admin API's authorizer, routes and
# integrations, and to the two functions.
```

with:

```hcl
# What it does not see, the most important first. The gate decides who gets a
# token; it does not decide what a token is worth. The scoreboard's admin API
# does, and it is a second authorization root: section 11 watches it, as a
# separate rule, so its alert names the authorizer and routes rather than the
# invite list.
```

- [ ] **Step 3: Append section 11**

Append to the end of `terraform/security-alarms.tf`:

```hcl

# ---- 11. The scoreboard admin API ----
#
# Section 10 watches the gate that decides who gets a token. This watches what
# decides what a token is worth. The scoreboard's scoreboard-api and
# scoreboard-enroll functions take the caller's identity entirely from the
# claims API Gateway's JWT authorizer hands them -- sub, and for claiming a
# panel cognito:username, email and email_verified -- and that authorizer's
# issuer and audience decide whose tokens are believed. So the API and the two
# functions are an authorization root of their own, and a write to them can
# claim or control panels without going near the pool, the gate or the invite
# list:
#
#   Point the authorizer elsewhere          -> UpdateAuthorizer, CreateAuthorizer.
#                                              An issuer the attacker runs mints
#                                              any sub, or the owner's verified
#                                              email.
#   Move a route, or repoint an integration -> UpdateRoute, CreateRoute,
#                                              UpdateIntegration. Removing a
#                                              route's authorizer alone is not
#                                              enough: the handler returns 401
#                                              with no sub.
#   Change either function                  -> UpdateFunctionCode*,
#                                              UpdateFunctionConfiguration*
#                                              (including its role), AddPermission*,
#                                              CreateFunctionUrlConfig.
#   Reshape or reroute the API              -> UpdateStage, UpdateApi,
#                                              CreateApiMapping, DeleteApi.
#
# Scoped by resource for the same reason as section 10: the account runs three
# other HTTP APIs (davidjdrake-api, healthtracker-api and HttpApi) and many
# other functions. It has no eventName constraint.
#
# Which fields name them comes from the apigatewayv2 and lambda service models
# shipped with aws-cli 2.33.2, cased as CloudTrail records them. Ninety days of
# real events, to 2026-09-15, confirmed apiId and resource-arn:
#
#   apiId         every write to the API and everything under it: routes,
#                 integrations, authorizers, stages, deployments, models, CORS,
#                 API mappings (35 operations outside Get/List take it)
#   resource-arn  TagResource/UntagResource, arn:aws:apigateway:<region>::/apis/<id>
#                 and anything under that ARN, so it is matched by prefix. The
#                 hyphen is the wire name, not a typo.
#   functionName  every Lambda write that takes a function; matched by wildcard,
#                 because CloudTrail records it both bare and as a full ARN
#   resource      Lambda TagResource/UntagResource, an ARN
#
# Measured before choosing, over ninety days to 2026-09-15: 1,427 API Gateway
# events, 1,340 of them reads; 5 writes to scoreboard-api and 4 to
# scoreboard-enroll, all the scoreboard's own applies. Most of those code
# updates changed no code: Go stamped each binary with its commit, so every
# commit redeployed every function. The scoreboard's make build now passes
# -buildvcs=false and -trimpath, so an unchanged function keeps its code hash
# and an apply pages here only when code moves. Reads stay silent for section
# 9's reason: an ENABLED rule never receives read-only management events.
#
# The API ID is looked up by name, with a precondition that exactly one API
# has it, like section 10's pool: a replaced API is picked up at this
# repository's next apply, and until then writes to the new API page nobody.
# The replacement's DeleteApi on the old ID is the one write that pages in that
# window. The function names are literals, like section 10's.
#
# What it does not see:
#   - A custom domain rerouted away from the API. DeleteApiMapping names only the
#     domain and the mapping, and routing rules and UpdateDomainName name only the
#     domain. The admin API has no custom domain today, so none of these can reach
#     it yet. Adding one means adding its domainName here.
#   - The two functions' IAM roles. HockeyTrack's identity rule excludes role-policy
#     churn deliberately, but scoreboard-enroll's role can mint device
#     certificates, so a widened grant there is a real route this does not watch.
#   - The devices table's ownership rows. Writes to them are DynamoDB data events,
#     which the trail does not log.
#   - The static site's bucket and distribution.
#   - A call that names these resources only through a field not listed above,
#     the silent-failure mode sections 8 to 10 share.
#   - Rewriting this rule, which section 9 catches.
data "aws_apigatewayv2_apis" "scoreboard_admin" {
  name = "scoreboard-admin"
}

locals {
  scoreboard_api_ids = tolist(data.aws_apigatewayv2_apis.scoreboard_admin.ids)

  scoreboard_api_pattern = jsonencode({
    "detail-type" = ["AWS API Call via CloudTrail"]
    "detail" = {
      "eventSource" = ["apigateway.amazonaws.com", "lambda.amazonaws.com"]
      "readOnly"    = [false]
      "$or" = [
        { "requestParameters" = { "apiId" = local.scoreboard_api_ids } },
        { "requestParameters" = { "resource-arn" = [for id in local.scoreboard_api_ids : { "prefix" = "arn:aws:apigateway:${var.region}::/apis/${id}" }] } },
        { "requestParameters" = { "functionName" = [{ "wildcard" = "*scoreboard-api*" }, { "wildcard" = "*scoreboard-enroll*" }] } },
        { "requestParameters" = { "resource" = [{ "wildcard" = "*:function:scoreboard-api*" }, { "wildcard" = "*:function:scoreboard-enroll*" }] } },
      ]
    }
  })
}

resource "aws_cloudwatch_event_rule" "scoreboard_api" {
  name          = "hockeytrack-sec-scoreboard-api"
  description   = "Any write naming the scoreboard admin API or the scoreboard-api or scoreboard-enroll function: the routes to changing what the API believes about a caller"
  event_pattern = local.scoreboard_api_pattern

  lifecycle {
    precondition {
      condition     = length(local.scoreboard_api_ids) == 1
      error_message = "Expected exactly one API Gateway API named scoreboard-admin, found ${length(local.scoreboard_api_ids)}. The scoreboard API rule would watch the wrong API, or none."
    }
    precondition {
      condition     = length(local.scoreboard_api_pattern) <= 2048
      error_message = "The scoreboard API rule's event pattern is ${length(local.scoreboard_api_pattern)} characters. EventBridge rejects patterns over 2048, and only at apply."
    }
  }
}
```

The spec's §4.1 sketch used `one()` inside the pattern. This uses a `for` expression and `tolist`, so zero or several matches reach the precondition and its readable message, not a `one()` error. Task 2 reconciles the spec.

- [ ] **Step 4: Format and validate**

Run: `cd /home/jay/projects/hockeytrack/terraform && export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime" && terraform fmt && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Threat model §4**

In `docs/threat-model.md`, replace:

```markdown
The gate is not
the only authorization root, though, and this rule does not watch the other:
the admin API that consumes the tokens takes the caller's identity entirely
from API Gateway's JWT authorizer, so changing which issuer that authorizer
trusts, its routes or integrations, or the code of the two functions behind
them can claim or control panels without touching the gate, and no rule in
either repository watches any of that today.
```

with:

```markdown
The gate is not
the only authorization root, though; the admin API that consumes the tokens is
the other, and the next paragraph covers it.

**Changing the scoreboard's admin API pages someone.** The gate decides who
gets a token; the admin API decides what a token is worth. Its two functions
take the caller's identity entirely from API Gateway's JWT authorizer, so
changing which issuer that authorizer trusts, moving a route or repointing an
integration, or changing either function's code, configuration or permissions
can claim or control panels without touching the gate. A second rule fires on
any write that names the `scoreboard-admin` API, in `apiId` or by ARN, or either
function. Like the sign-in rule, it lists no event names and is scoped to the
scoreboard's resources, because the account runs three other HTTP APIs. What it
does not see is named in its comment: a custom domain rerouted away from the
API (there is none today), the functions' IAM roles, and the devices table's
ownership rows, whose writes the trail does not log. It pages only when code
changes, because the scoreboard's build no longer stamps each binary with its
commit.
```

- [ ] **Step 6: Threat model §7**

In the "A scoreboard sign-in alert you cannot account for." entry, replace:

```markdown
8. The admin API, which the alert's rule does not watch. Find the
```

with:

```markdown
8. The admin API, which a separate rule watches. Check it anyway: find the
```

Then replace:

```markdown
**The archive has lost objects.** Do not write anything to the bucket. Every
```

with:

```markdown
**A scoreboard admin API alert you cannot account for.** Assume someone can make
the admin API believe a caller it should not, and with it claim or control
panels. The Actor line names the credential, and the root sign-in procedure
applies to it. Then, in us-east-1:
1. `aws apigatewayv2 get-apis` to find `scoreboard-admin`'s ID, then
   `get-authorizers --api-id <id>`. There must be exactly one JWT authorizer,
   whose issuer is `https://cognito-idp.us-east-1.amazonaws.com/<pool id>` and
   whose audience is the site client's ID alone, as the scoreboard repository's
   `terraform/admin.tf` sets them.
2. `get-routes --api-id <id>`. Every route must use that authorizer except
   `POST /api/enroll` and `GET /api/enroll`, which are unauthenticated by
   design (`terraform/enroll.tf`), and no route may exist that the scoreboard
   repository does not define.
3. `get-integrations --api-id <id>`. Every `IntegrationUri` must be the
   `scoreboard-api` or `scoreboard-enroll` function.
4. For each function, run `aws lambda get-function`, `get-function-configuration`
   and `get-policy`, then `list-function-url-configs` and
   `list-event-source-mappings`:
   - the code SHA must match a fresh `make build`;
   - the role must be the scoreboard's own;
   - the resource policy must allow only API Gateway, from this API's
     execution ARN;
   - there must be no function URL and no event source mapping.
5. Check which panels changed hands, as in the sign-in entry's step 4, by
   scanning `scoreboard-devices` for owners who are not invited users.
6. Run a plan in the scoreboard repository. Anything rewritten shows as a
   difference, and applying puts it back.

**The archive has lost objects.** Do not write anything to the bucket. Every
```

- [ ] **Step 7: Commit**

```bash
cd /home/jay/projects/hockeytrack
git add terraform/security-alarms.tf docs/threat-model.md
git commit -m "security: page on any change to the scoreboard's admin API

The sign-in gate decides who gets a token; the admin API decides what a
token is worth. Its two functions take identity entirely from API
Gateway's JWT authorizer, so a change to that authorizer, a route, an
integration, or either function could claim or control panels without
touching the gate, and nothing paged on it.

Section 11 matches any write naming the scoreboard-admin API, in apiId or
by ARN prefix, or either function. The fields come from the apigatewayv2
and lambda service models. It has no eventName constraint, and it is
scoped by resource because the account runs three other HTTP APIs. The
API is looked up by name, with an exactly-one precondition. What it does
not see is named: a custom domain rerouted away (there is none), the
functions' IAM roles, and the devices table's data plane.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 2: Reproducible builds and doc closure (hockeytrack-scoreboard)

**Files:**
- Modify: `/home/jay/projects/hockeytrack-scoreboard/Makefile` (the `build` target)
- Create: `/home/jay/projects/hockeytrack-scoreboard/site/tests/build-config.test.js`
- Modify: `/home/jay/projects/hockeytrack-scoreboard/docs/superpowers/specs/2026-09-14-signin-detection-design.md` (§6)
- Modify: `/home/jay/projects/hockeytrack-scoreboard/docs/superpowers/specs/2026-09-15-api-detection-design.md` (§4.1, §4.2)

**Interfaces:**
- Consumes: nothing from Task 1 at build time.
- Produces: `make build` output that is byte-identical across commits for unchanged code. Task 3 proves it.

Work on the existing branch `api-detection`.

- [ ] **Step 1: Write the failing tripwire**

Create `site/tests/build-config.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Lambda deploys are driven by the zip's hash, and HockeyTrack's security rules
// page on every Lambda code update. Go stamps each binary with the commit it
// was built at (-buildvcs) and with the checkout's absolute path (unless
// -trimpath), so without both flags every commit -- or a build from another
// directory -- changes every function's hash, redeploys code that did not
// change, and pages for nothing. Once people learn to ignore those pages, a
// real one is ignored too. This keeps the build reproducible.
const makefile = readFileSync(new URL("../../Makefile", import.meta.url), "utf8");

test("every Lambda build is reproducible across commits and checkouts", () => {
  const builds = makefile.split("\n").filter((line) => /\$\(GO\) build\b/.test(line));
  assert.ok(builds.length >= 5, `found only ${builds.length} Lambda build lines`);
  for (const line of builds) {
    assert.match(line, /(^|\s)-buildvcs=false(\s|$)/, `missing -buildvcs=false: ${line.trim()}`);
    assert.match(line, /(^|\s)-trimpath(\s|$)/, `missing -trimpath: ${line.trim()}`);
  }
});
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd /home/jay/projects/hockeytrack-scoreboard/site && node --test tests/build-config.test.js`
Expected: FAIL with `missing -buildvcs=false`.

- [ ] **Step 3: Add the flags**

In `Makefile`, in every line of the `build` target containing `$(GO) build -ldflags="-s -w"`, replace `$(GO) build -ldflags="-s -w"` with `$(GO) build -buildvcs=false -trimpath -ldflags="-s -w"`. There are five lines: reducer, today, api, enroll and authgate.

Add this comment immediately above the `build:` line, keeping the existing comment before it:

```make
# -buildvcs=false and -trimpath make the binaries reproducible: Go otherwise
# stamps each one with the commit and the checkout's absolute path, so every
# commit changes every function's hash, and Terraform redeploys -- and
# HockeyTrack's security rules page on -- code that did not change.
# site/tests/build-config.test.js keeps both flags on every build line.
```

- [ ] **Step 4: Run it and confirm it passes; prove reproducibility locally**

Run: `cd /home/jay/projects/hockeytrack-scoreboard && make test-js`
Expected: all site tests pass, including the new one.

Then: `make build && sha256sum build/*/bootstrap > /tmp/claude-1000/build-a.sha && touch cloud/go.mod && make build && sha256sum build/*/bootstrap | diff - /tmp/claude-1000/build-a.sha && echo reproducible`
Expected: `reproducible`. The before-and-after-commit comparison is Task 3's job; this only proves a rebuild is stable. Write the scratch file under `/tmp/claude-1000/` and delete it afterwards.

- [ ] **Step 5: Close the gap in the sign-in detection spec**

In `docs/superpowers/specs/2026-09-14-signin-detection-design.md`, replace:

```markdown
- **The admin API's authorization, the most important gap this leaves.** The
```

with:

```markdown
- **The admin API's authorization, the most important gap this left.** Closed
  2026-09-15 by `2026-09-15-api-detection-design.md` (HockeyTrack section 11).
  The
```

- [ ] **Step 6: Reconcile the API detection spec with the plan**

In `docs/superpowers/specs/2026-09-15-api-detection-design.md`:
- In the §4.1 HCL block, replace the `resource-arn` line with:

  ```hcl
      { "requestParameters" = { "resource-arn" = [for id in local.scoreboard_api_ids : { "prefix" = "arn:aws:apigateway:${var.region}::/apis/${id}" }] } },
  ```
- Replace the sentence beginning `` `one()` in the pattern fails the plan by itself`` with: `` A `for` expression over the looked-up IDs, rather than `one()`, lets zero or several matches reach the precondition's readable message instead of failing inside the pattern.``
- In §4.2, replace `` `make build` passes `-buildvcs=false` to every `go build`. `` with `` `make build` passes `-buildvcs=false` and `-trimpath` to every `go build`: Go otherwise stamps the commit and the checkout's absolute path into each binary. ``

- [ ] **Step 7: Commit**

```bash
cd /home/jay/projects/hockeytrack-scoreboard
git add Makefile site/tests/build-config.test.js docs/superpowers/specs/2026-09-14-signin-detection-design.md docs/superpowers/specs/2026-09-15-api-detection-design.md
git commit -m "build: stop redeploying functions whose code did not change

Go stamped each Lambda binary with its commit and the checkout's path,
so every commit changed every function's hash. Terraform then
redeployed all five functions on every apply, and HockeyTrack's
security rules paged on each redeploy. With -buildvcs=false and
-trimpath, an unchanged function builds to the same bytes, so an
apply, and a page, happens only when code changes. A tripwire keeps both
flags on every build line.

Also marks the admin API gap closed in the sign-in detection spec, and
reconciles the API detection spec with the plan.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 3: Prove, deploy and break (controller and user, not a subagent)

The controller does every read-only step and hands every write to the user, who runs it with `!` or tells the controller to run it. Every AWS command passes `--region us-east-1`. Scratch files go in the session scratchpad, which is `<scratchpad>` below.

- [ ] **Step 1: Plan HockeyTrack**

Read the deployed image tag first: `aws lambda get-function --region us-east-1 --function-name hockeytrack-poller --query Code.ImageUri --output text`. Then run `terraform plan -input=false -var="image_tag=<that tag>" -out=<scratchpad>/ht-api.tfplan` and read it with `terraform show` (never `-json`).

Expected: `+` `aws_cloudwatch_event_rule.scoreboard_api` and `+` `aws_cloudwatch_event_target.security["scoreboard_api"]`; `~` the topic policy and the queue policy (one more SourceArn each). Nothing else. The plan output shows the rendered pattern with the resolved API ID, which must be `dk3k7p41e2`.

- [ ] **Step 2: Prove the pattern against real events before applying**

Rebuild the exact pattern from the plan's rendering into `<scratchpad>/api-pattern.json`, compact with sorted keys, and record its length (it must be at most 2,048). Then run a `test-event-pattern` harness over real `lookup-events` records wrapped in EventBridge envelopes, the same harness shape as in the sign-in detection plan.

- **Must match:**
  - on `dk3k7p41e2`: `CreateRoute`, `CreateAuthorizer`, `CreateIntegration`, `CreateStage` and `UpdateStage`;
  - for each function: `CreateFunction20150331` and `AddPermission20150331v2`;
  - on `scoreboard-api`, a real `UpdateFunctionCode20150331v2` and a real `UpdateFunctionConfiguration20150331v2` from the 90 days, in both the bare and the ARN `functionName` forms if both occur.
- **Must not match:**
  - `GetRoute` and `GetAuthorizer` on `dk3k7p41e2`;
  - `CreateRoute` on `bjmqkenm95` (healthtracker-api);
  - `TagResource` on `op8gfgqr8f`;
  - `CreateAuthorizer` on `op8gfgqr8f`;
  - `UpdateFunctionCode20150331v2` on `scoreboard-authgate`;
  - a HealthTracker Lambda write, if one exists in the window;
  - a `TagResource20170331v2` on a HealthTracker Lambda, if one exists in the window.

Then sweep 90 days of `apigateway` and `lambda` events with a local matcher. Name every match by event name and date: each must be a change to the admin API or one of the two functions. Spot-check three matches and three non-matches with `test-event-pattern`, and list any event naming these resources that has no `readOnly` key.

- [ ] **Step 3: The user applies HockeyTrack**

Hand the user the apply of the saved plan. Then:
- `terraform plan -detailed-exitcode` must exit 0;
- `describe-rule` must return a pattern equal to the rendered one as parsed JSON, in state `ENABLED`;
- `list-targets-by-rule` must show the security topic with its DLQ.

Expected emails: the apply writes resources section 9 (`hockeytrack-sec-alerting-modification`) names, so it pages once each for `PutRule` (the new rule), `PutTargets` (its target) and `SetTopicAttributes` (the topic policy). The queue policy is an SQS write, which section 9 does not watch. No section 11 email is expected, and any other email is a finding to explain before going on.

- [ ] **Step 4: Prove reproducible builds across a commit, then deploy them once**

1. Build at two commits in two throwaway clones, never in the working tree: `git clone /home/jay/projects/hockeytrack-scoreboard <scratchpad>/repro-a`, the same into `<scratchpad>/repro-b`, then `git -C <scratchpad>/repro-a checkout --detach origin/api-detection` and `git -C <scratchpad>/repro-b checkout --detach origin/api-detection~1`. Both commits carry Task 2's flags. Confirm their Go code is identical with `git -C <scratchpad>/repro-a diff --quiet origin/api-detection~1 origin/api-detection -- cloud` (exit 0) and that the Makefile's `go build` lines match; if either differs, use two docs-only commits on the branch instead. The clones' different absolute paths also exercise `-trimpath`.
2. Run `make build` in each clone, then compare `sha256sum build/*/bootstrap` across the two. Expected: all five identical. Record `go version -m build/api/bootstrap` from one clone: it shows the Go and module versions and no `vcs.` lines. Terraform's `archive_file` re-zips each bootstrap with a pinned mode, so identical bootstraps mean identical code hashes. Delete both clones.
3. Run `terraform plan -out=<scratchpad>/sb-api.tfplan`. Expected: the five functions update once, because their binaries lose the stamps, and nothing else changes. Hand the user the apply.
4. Expected emails: section 11 pages `UpdateFunctionCode20150331v2` for `scoreboard-api` and `scoreboard-enroll`, and section 10 pages it for `scoreboard-authgate`. These are the first real positives for the `functionName` branch. The reducer and today redeploys page nothing. Each api and enroll email's body must carry section 11's sentence ("If this was not you, assume the scoreboard admin API may accept tokens or requests it should not. …"), and the authgate email section 10's ("If this was not you, assume the scoreboard admin site's sign-in gate may be bypassed. …"). Then check the security DLQ depth, which must be 0.
5. `terraform plan -detailed-exitcode` must exit 0. Then make a docs-only commit, run `make build`, and run `terraform plan -detailed-exitcode -out=<scratchpad>/sb-docs.tfplan`. Expected: no changes, with exit 0. Hand the user the apply of that saved plan anyway, because an apply is what pages: it must report 0 added, 0 changed, 0 destroyed, and no rule email may arrive.

- [ ] **Step 5: Break each branch**

Hand these to the user one at a time. Check the Gmail inbox for `HOCKEYTRACK SECURITY` emails, each of whose bodies must carry section 11's sentence, and the security DLQ depth afterwards, which must be 0.

1. **`resource-arn`:**
   - `! aws apigatewayv2 tag-resource --region us-east-1 --resource-arn arn:aws:apigateway:us-east-1::/apis/dk3k7p41e2 --tags detection-probe=1`
   - then `untag-resource --resource-arn … --tag-keys detection-probe`.

   Expected: two emails.
2. **`apiId`:** read the stage's defaults with `aws apigatewayv2 get-stage --region us-east-1 --api-id dk3k7p41e2 --stage-name '$default' --query DefaultRouteSettings`, then:
   - `! aws apigatewayv2 update-stage --region us-east-1 --api-id dk3k7p41e2 --stage-name '$default' --default-route-settings ThrottlingBurstLimit=40,ThrottlingRateLimit=20`, using the values just read.

   Expected: an `UpdateStage` email, and a scoreboard plan still shows no drift.
3. **`resource`:** tag and untag `arn:aws:lambda:us-east-1:989232581535:function:scoreboard-api` and `…:scoreboard-enroll`. Expected: four emails.
4. **`functionName`:** re-save each function's current timeout, which is `10` for `scoreboard-api` and `15` for `scoreboard-enroll` (read them first):
   - `! aws lambda update-function-configuration --region us-east-1 --function-name scoreboard-api --timeout 10`
   - and the same for `scoreboard-enroll`.

   Expected: two emails.
5. **Negatives:** plans in both repositories, normal use of the admin site (sign in, panel list), and an enroll collect poll without a valid token send no rule email. The poll is `curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer not-a-token' "$(cd terraform && terraform output -raw api_endpoint)/api/enroll"`, which must print `404`.

- [ ] **Step 6: Run the new recovery entry's read-only steps once**

Follow HockeyTrack's threat model §7 entry "A scoreboard admin API alert you cannot account for", running only its reads, each with `--region us-east-1`:
- `get-authorizers`, `get-routes` and `get-integrations` on `dk3k7p41e2`;
- for both functions, `get-function`, `get-function-configuration`, `get-policy`, `list-function-url-configs`, `list-event-source-mappings`, `list-aliases` and `list-versions-by-function`;
- for both roles, `iam get-role`, `list-role-policies`, `get-role-policy` and `list-attached-role-policies`;
- `CodeSha256` for both functions against `openssl dgst -sha256 -binary build/api.zip | base64` and `build/enroll.zip`, after `make build` and a `terraform plan` that regenerates the zips;
- `aws iot list-certificates`, and `list-principal-things` for one known panel's certificate.

Every expected value the entry states must match what AWS returns. Correct any command, field name or expected output that does not, in a small follow-up commit to HockeyTrack's `docs/threat-model.md`. None of the entry's writes (sign-outs, revocations, deletions) are run.

- [ ] **Step 7: Record and hand off**

Append `## 7. Verification record (2026-09-15)` to `docs/superpowers/specs/2026-09-15-api-detection-design.md`, recording:
- the pattern's length;
- every must-match and must-not-match result;
- the sweep's counts and matches;
- the reproducibility result;
- the one-time redeploy and the three emails it produced;
- each break and its email;
- the negatives;
- the drift checks;
- the read-only recovery run and any correction it forced;
- that direct invocation of the two functions, their roles and trust policies, and the API's and functions' log groups are known and unwatched.

Commit it, then offer the user the finishing choice for both branches.
