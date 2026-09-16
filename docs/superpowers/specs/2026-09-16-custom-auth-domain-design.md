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

## 7. Verification record (2026-09-16)

All times UTC.

### Before applying

- **Review changed three things.**
  - A content-security-policy test that would still have passed if the Cognito suffix crept back now fails on any mention of `amazoncognito.com`.
  - The manual `curl` walkthrough in `docs/admin-api.md` no longer appends the suffix to an output that is already the full host.
  - A precondition checks that the zone's apex has an A record, since Cognito refuses a custom domain otherwise.
- **The precondition failed the first real plan.** Route 53's record sets carry a trailing dot (`davidjdrake.com.`) and the zone data source renders its name without one, so the check said "no apex record" about a zone that has one. Both sides are now compared with the dot trimmed. A reviewer had reported the check true against the live zone, but had read the AWS CLI's value rather than the data source's; the plan is what settled it.
- **Plan:** `5 to add, 1 to change, 1 to destroy`. The domain replaced (`scoreboard-admin-989232581535` → `auth.scoreboard.davidjdrake.com`), the certificate, its validation record and the alias created, and the response-headers policy's `connect-src` updated. Nothing touching the pool, its clients, the identity provider or the functions.

### Google, before the apply (21:22)

In the OAuth client, `https://auth.scoreboard.davidjdrake.com/oauth2/idpresponse` was added as a redirect URI and `https://auth.scoreboard.davidjdrake.com` as a JavaScript origin, with the Cognito entries kept. Google confirmed the save, and a reload showed all four. The branding page already carried the app name, support email, home page, privacy policy and `davidjdrake.com` as an authorized domain, so nothing changed there.

### Applied 21:23:20

The domain took 2 minutes 49 seconds to create and the alias 31 seconds more — about four minutes of sign-in outage in all, well under the 15 to 20 the spec allowed for. Cognito reported the domain `ACTIVE`, and the hosted sign-in page answered 200.

**One correction to §5:** the spec proposed checking `https://auth.scoreboard.davidjdrake.com/.well-known/jwks.json`, which returns 404. Cognito publishes signing keys on `cognito-idp.us-east-1.amazonaws.com`, not on the hosted-UI domain, so the sign-in page answering is the right check.

### The site and the sign-in

- **`make site`, run by the owner,** wrote `"cognitoDomain": "auth.scoreboard.davidjdrake.com"` into the live `config.json`, and the served policy's `connect-src` names the same host.
- **The first sign-ins on the new domain succeeded but showed no consent screen,** because Google remembered the grant the account had already given this client. Deleting the app from the account's linked apps did not take on the first attempt — the list still showed it — and did on the second.
- **The consent screen then read "Sign in to davidjdrake.com"** — "Google will allow davidjdrake.com to access this info about you: Email address" — with the privacy policy link on `scoreboard.davidjdrake.com/privacy/`. No AWS hostname, no account number.
- **Continuing signed the owner in.** The gate ran on every sign-in with no refusals or errors, the admin API answered `GET /api/devices` and `GET /api/games` with 200, and `scoreboard-signin-refused`, `scoreboard-authgate-failures`, `scoreboard-authgate-throttles` and `scoreboard-token-mismatch` all stayed OK.

**It says "davidjdrake.com" rather than "HockeyTrack Scoreboard"** because Google shows an app's name only once its branding is verified. That remains optional and is not part of this change.

### Google, after the switch (21:38)

The Cognito redirect URI and JavaScript origin were removed from the client, and the Cognito host from the authorized domains. Each save was confirmed by reloading the page: the client holds only the new domain's two entries, and `davidjdrake.com` is the only authorized domain. Google now refuses to delete `davidjdrake.com`, because the client uses it.

### Detection and drift

`hockeytrack-sec-scoreboard-signin` (section 10) matched 2 events in the 21:21 window — the domain's deletion and creation — which is the expected noise. The dead-letter queue is at 0, and `terraform plan -detailed-exitcode` exits 0 in both repositories.
