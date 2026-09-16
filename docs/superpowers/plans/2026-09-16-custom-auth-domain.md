# Custom Sign-In Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Cognito prefix domain with `auth.scoreboard.davidjdrake.com`, so Google's consent screen names `davidjdrake.com` instead of an AWS hostname carrying the account ID.

**Architecture:** A DNS-validated ACM certificate for the subdomain, the existing `aws_cognito_user_pool_domain` switched to a custom domain, and a Route 53 alias to the distribution Cognito creates. The site learns the new host from the `cognito_domain` output that `make site` already writes into `site/config.json`.

**Tech Stack:** Terraform (AWS provider), ACM, Route 53, Cognito hosted UI, Node `node:test`.

**Spec:** `docs/superpowers/specs/2026-09-16-custom-auth-domain-design.md`

## Global Constraints

- **Repository is PUBLIC.** Never read, modify or commit `terraform/terraform.tfvars`. No credentials or real email addresses.
- **Agents never run** `terraform apply`, `terraform plan`, any mutating AWS call, `make deploy`, `make site`, `make provision`, or `git push`. `terraform -chdir=terraform fmt -check`, `terraform -chdir=terraform validate`, `make test` and read-only `aws ... --region us-east-1` calls are allowed.
- **Every AWS CLI command passes `--region us-east-1`.** US spelling.
- **The domain is `auth.${var.site_domain}`** — do not write the literal `auth.scoreboard.davidjdrake.com` into Terraform; `var.site_domain` is already `scoreboard.davidjdrake.com`.
- **The certificate must be in us-east-1**, which is this provider's only region.
- **Branch:** `custom-auth-domain` in `/home/jay/projects/hockeytrack-scoreboard` (checked out; spec committed).
- **Do not change** the user pool, its clients, the identity provider, the gate's triggers, or anything about who may sign in.

## File map

| File | Responsibility |
|---|---|
| `terraform/admin.tf` (modify) | `aws_cognito_user_pool_domain.admin` becomes a custom domain |
| `terraform/signin.tf` (modify) | The certificate, its validation records, and the alias record — sign-in's own file, beside the identity provider |
| `terraform/site.tf` (modify) | CSP `connect-src`; the `cognito_domain` output |
| `site/tests/signin-config.test.js` (modify) | Two tripwires |

---

### Task 1: The custom domain

**Files:**
- Modify: `terraform/admin.tf` (the `aws_cognito_user_pool_domain "admin"` resource, around line 97)
- Modify: `terraform/signin.tf` (append)
- Modify: `terraform/site.tf` (the CSP's `connect-src`, around line 155; the `cognito_domain` output, around line 273)
- Modify: `site/tests/signin-config.test.js`

**Interfaces:**
- **Consumes:** `var.site_domain` (`scoreboard.davidjdrake.com`), `data.aws_route53_zone.site` (declared in `site.tf`), `aws_cognito_user_pool.admin`.
- **Produces:** `aws_cognito_user_pool_domain.admin` as a custom domain whose `.domain` is the full host, and `.cloudfront_distribution` for the alias record.

- [ ] **Step 1: Write the failing tripwires**

In `site/tests/signin-config.test.js`, append:

```js
test("the hosted UI answers on this project's own domain, not a Cognito prefix", () => {
  const domain = code(block(admin, 'resource "aws_cognito_user_pool_domain" "admin" {'));
  assert.match(domain, /domain\s*=\s*"auth\.\$\{var\.site_domain\}"/,
    "the pool's domain must be auth.<site domain>; a prefix domain puts the AWS hostname and the account ID on Google's consent screen");
  assert.match(domain, /certificate_arn\s*=\s*aws_acm_certificate_validation\.auth\.certificate_arn/,
    "a custom domain needs its validated certificate");
});

test("the page may reach the same sign-in host the site is configured with", () => {
  const site = readFileSync(new URL("../../terraform/site.tf", import.meta.url), "utf8");
  const csp = code(site).match(/connect-src[^;]*;/);
  assert.ok(csp, "connect-src not found in the CSP");
  assert.match(csp[0], /https:\/\/\$\{aws_cognito_user_pool_domain\.admin\.domain\}/,
    "connect-src must name the pool's domain as written, or it drifts from the output the site is built with");
  const out = code(site).match(/output\s+"cognito_domain"\s*\{[^}]*\}/);
  assert.ok(out, "the cognito_domain output not found");
  assert.match(out[0], /value\s*=\s*aws_cognito_user_pool_domain\.admin\.domain/);
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd site && node --test tests/signin-config.test.js`
Expected: both new tests FAIL — the domain is still `scoreboard-admin-${account}`, and the CSP still appends `.auth.${var.region}.amazoncognito.com`.

- [ ] **Step 3: Make the pool's domain a custom one**

In `terraform/admin.tf`, replace the `aws_cognito_user_pool_domain "admin"` resource with:

```hcl
# The hosted UI answers here, and this is the domain Google shows on its
# consent screen. A Cognito prefix domain would put
# <prefix>.auth.<region>.amazoncognito.com there instead, which names AWS and
# this account's ID on the one page a person reads before handing over their
# Google identity (SCO-27). Changing this replaces the resource, and sign-in
# is unavailable while the new domain is built -- panels are unaffected,
# because they hold IoT certificates, not Cognito sessions.
resource "aws_cognito_user_pool_domain" "admin" {
  domain          = "auth.${var.site_domain}"
  certificate_arn = aws_acm_certificate_validation.auth.certificate_arn
  user_pool_id    = aws_cognito_user_pool.admin.id
}
```

- [ ] **Step 4: Add the certificate and the alias record**

At the end of `terraform/signin.tf`:

```hcl
# The hosted UI's certificate. ACM must hold it in us-east-1 for Cognito's
# distribution, which is this provider's region, and the pattern is site.tf's:
# request, prove ownership with a DNS record in the zone, then wait.
resource "aws_acm_certificate" "auth" {
  domain_name       = "auth.${var.site_domain}"
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "auth_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.auth.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
  zone_id         = data.aws_route53_zone.site.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "auth" {
  certificate_arn         = aws_acm_certificate.auth.arn
  validation_record_fqdns = [for r in aws_route53_record.auth_cert_validation : r.fqdn]
}

# Cognito builds its own CloudFront distribution for the custom domain and
# hands back its hostname; this points the name at it. Z2FDTNDATAQYW2 is
# CloudFront's fixed hosted-zone ID, the same for every distribution.
resource "aws_route53_record" "auth" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = aws_cognito_user_pool_domain.admin.domain
  type    = "A"

  alias {
    name                   = aws_cognito_user_pool_domain.admin.cloudfront_distribution
    zone_id                = "Z2FDTNDATAQYW2"
    evaluate_target_health = false
  }
}
```

- [ ] **Step 5: Point the CSP and the output at the domain as written**

In `terraform/site.tf`, in the CSP, replace:

```
"connect-src 'self' ${aws_apigatewayv2_api.admin.api_endpoint} https://${aws_cognito_user_pool_domain.admin.domain}.auth.${var.region}.amazoncognito.com; ",
```

with:

```
"connect-src 'self' ${aws_apigatewayv2_api.admin.api_endpoint} https://${aws_cognito_user_pool_domain.admin.domain}; ",
```

and replace the `cognito_domain` output's value:

```
  value       = "${aws_cognito_user_pool_domain.admin.domain}.auth.${var.region}.amazoncognito.com"
```

with:

```
  value       = aws_cognito_user_pool_domain.admin.domain
```

Leave its `description` as it is.

- [ ] **Step 6: Run the tests and the Terraform checks**

Run:
```bash
cd /home/jay/projects/hockeytrack-scoreboard
make test
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```
Expected: all tests pass, `fmt` silent (run `terraform -chdir=terraform fmt` and re-check if not), `validate` prints "Success!". Do not run `plan`.

- [ ] **Step 7: Check for anything else naming the old host**

Run: `grep -rn "amazoncognito" --include=* . | grep -v '\.git/' | grep -v docs/superpowers`
Every remaining hit must be either documentation describing history, or a test fixture. Report what you found and what you left alone; change nothing under `docs/superpowers/`.

- [ ] **Step 8: Commit**

```bash
git add terraform/admin.tf terraform/signin.tf terraform/site.tf site/tests/signin-config.test.js
git commit -m "feat: sign in on this project's own domain

The hosted UI moves to auth.<site domain> with its own certificate, so
Google's consent screen names davidjdrake.com instead of a Cognito prefix
host carrying the AWS account ID (SCO-27). The site picks the new host up
from the cognito_domain output that make site already writes into config.

Co-Authored-By: <your model> <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 2: Console steps, apply and proof (controller and user; no subagent)

- [ ] **Step 1: The user's Google console work, before anything is applied**

In the OAuth client: add `https://auth.scoreboard.davidjdrake.com/oauth2/idpresponse` as an authorized redirect URI, keeping the existing one. On the consent screen: authorized domain `davidjdrake.com`, plus the app name and support email. Confirm before continuing — applying first means a longer outage.

- [ ] **Step 2: Plan**

```bash
cd /home/jay/projects/hockeytrack-scoreboard
make build
terraform -chdir=terraform plan -input=false -no-color -out=<scratchpad>/auth-domain.tfplan
terraform -chdir=terraform show -no-color <scratchpad>/auth-domain.tfplan | grep -E "will be|must be|Plan:"
```

Expected: `aws_cognito_user_pool_domain.admin` replaced; `aws_acm_certificate.auth`, its validation record, `aws_acm_certificate_validation.auth` and `aws_route53_record.auth` created; the CloudFront distribution updated for the CSP. Nothing touching the pool, its clients, the identity provider or the functions.

- [ ] **Step 3: The user applies,** then the domain is watched until it answers:

```bash
aws cognito-idp describe-user-pool-domain --region us-east-1 --domain auth.scoreboard.davidjdrake.com --query 'DomainDescription.[Status,CloudFrontDistribution]' --output text
curl -s -o /dev/null -w "%{http_code}\n" https://auth.scoreboard.davidjdrake.com/.well-known/jwks.json
```

Expected: `ACTIVE`, and `200` once DNS has propagated. Fifteen to twenty minutes is normal.

- [ ] **Step 4: The user runs `make site`** so `site/config.json` carries the new host, then signs in.

Expected: the consent screen names `davidjdrake.com`; the panel list loads. If sign-in fails, read `/aws/lambda/scoreboard-authgate` and the site's console — the likely cause is the Google redirect URI not matching.

- [ ] **Step 5: Detection and health**

- Section 10's alert for the domain change arrives (`CreateUserPoolDomain`, `DeleteUserPoolDomain`), which is this apply's expected noise.
- `scoreboard-signin-refused`, `scoreboard-authgate-failures` and `scoreboard-token-mismatch` stay OK.
- The dead-letter queue stays at 0.

- [ ] **Step 6: The user removes the old redirect URI** from the Google client, once sign-in is proven on the new domain.

- [ ] **Step 7: Record and finish**

Append `## 7. Verification record (<date>)` to the spec: the plan, the apply, how long the domain took, the sign-in, the consent screen, the alerts, and drift checks in both repositories. Commit, then use superpowers:finishing-a-development-branch. Comment the outcome on SCO-27.
