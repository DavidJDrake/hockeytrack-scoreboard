# A Custom Sign-In Domain (SCO-27) — Design

**Status:** accepted (design approved in conversation, 2026-09-16)
**Date:** 2026-09-16
**Closes:** SCO-27. Google's consent screen shows `scoreboard-admin-989232581535.auth.us-east-1.amazoncognito.com`, which names AWS and the account number instead of this project.
**Repository:** this one. Terraform, the site's CSP and output, the sign-in tests, and console steps only the owner can take.

## 1. Purpose

The consent screen is the one page a person sees before handing over their Google identity, and it currently says an AWS hostname carrying the account ID. That is unprofessional, teaches the owner to ignore the domain on a consent screen, and leaks the account number to anyone who signs in.

Cognito's prefix domain is what puts it there. A custom domain replaces it with `auth.scoreboard.davidjdrake.com`, and Google then shows `davidjdrake.com`.

## 2. Facts established by investigation (2026-09-16, read-only)

1. **The pool has a prefix domain and no custom domain:** `scoreboard-admin-989232581535`.
2. **A user pool has one domain, not both.** Changing `aws_cognito_user_pool_domain.admin` replaces the resource, so sign-in is unavailable while the new domain is built — AWS gives no guarantee, and 15 to 20 minutes is the usual range. Panels are unaffected: they hold IoT certificates, not Cognito sessions.
3. **`davidjdrake.com` is a Route 53 hosted zone in this account** (`Z04202891HM5X7HAEVE8H`) with an apex A record, which Cognito requires before it will accept a custom domain under it.
4. **The site already learns the domain at build time.** `terraform/site.tf` outputs `cognito_domain`, and `make site` writes it into `site/config.json`, which `site/assets/auth.js` reads. No JavaScript changes.
5. **The certificate pattern already exists** in `site.tf`: `aws_acm_certificate` with DNS validation, validation records in Route 53, and `aws_acm_certificate_validation`.
6. **Nothing is billed for this.** ACM certificates are free, and Cognito runs the custom domain's distribution at no charge.

## 3. Design

### 3.1 Terraform

- **A certificate for `auth.${var.site_domain}`** — `auth.scoreboard.davidjdrake.com` — with DNS validation records in the existing zone, following `site.tf`'s pattern exactly.
- **`aws_cognito_user_pool_domain.admin` becomes the custom domain,** with `certificate_arn` from the validation resource and `depends_on` the apex A record's existence (already true, and checked by a precondition rather than created here: this stack does not own `davidjdrake.com`'s apex).
- **A Route 53 alias record** for `auth.scoreboard.davidjdrake.com` pointing at `aws_cognito_user_pool_domain.admin.cloudfront_distribution`, with the standard CloudFront hosted-zone ID `Z2FDTNDATAQYW2`.
- **The CSP's `connect-src`** in `site.tf` stops appending `.auth.<region>.amazoncognito.com` and uses the domain as written.
- **The `cognito_domain` output** becomes the custom domain, so `make site` writes the new host into `site/config.json`.

### 3.2 Tests

`site/tests/signin-config.test.js` gains two tripwires:

- The pool's domain resource sets `certificate_arn`, so a revert to a prefix domain fails the build.
- The CSP's `connect-src` names the same domain the output does, so the two cannot drift apart.

### 3.3 Console steps, in order

These are the owner's, and the order is what keeps the outage short.

1. **Before applying,** in the Google Cloud console's OAuth client: add `https://auth.scoreboard.davidjdrake.com/oauth2/idpresponse` as an authorized redirect URI, alongside the existing one. Under the consent screen, set the authorized domain to `davidjdrake.com`, and set the app name and support email.
2. **Apply,** and wait for the new domain to answer.
3. **Run `make site`,** which writes the new host into the site's configuration and uploads it.
4. **Sign in,** and confirm the consent screen names `davidjdrake.com`.
5. **Then remove** the old `scoreboard-admin-989232581535.auth.us-east-1.amazoncognito.com` redirect URI from the Google client.

Google does not need to re-verify the app: the sign-in requests `openid` and `email` only, which are not sensitive scopes.

## 4. What this does not change

- **Who may sign in.** The invite list, the pre sign-up gate and the pre token generation gate are untouched.
- **The tokens themselves.** Issuer, audience and the functions' verification all key off the user pool ID, not the domain.
- **The detection rules.** Section 10 pages on the domain change itself, because `CreateUserPoolDomain` and `DeleteUserPoolDomain` name the pool — expected noise for this apply, and worth confirming rather than assuming.

## 5. Testing

- **Before applying:** `make test`, and a plan showing the domain replaced, the certificate and validation created, and the alias record created — and nothing else.
- **After applying:** the domain's status is `ACTIVE`; `https://auth.scoreboard.davidjdrake.com/.well-known/jwks.json` answers; a real sign-in works; the consent screen shows `davidjdrake.com`; `site/config.json` carries the new host.
- **Detection:** section 10's alert for the domain change arrives, and the gate's alarms stay quiet.
- **After:** drift checks in both repositories, and a verification record in this spec.

## 6. Rollback

Re-apply the previous commit, which restores the prefix domain, and keep the old redirect URI in Google until the new domain is proven. The outage on rollback is the same 15 to 20 minutes.
