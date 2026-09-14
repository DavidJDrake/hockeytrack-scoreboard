# Sign in with Google — Design

**Status:** accepted (design approved in conversation, 2026-09-13)
**Date:** 2026-09-13
**Amends:** `2026-09-07-admin-site-design.md` §3.1 (identity) and
`2026-09-12-device-enrollment-design.md` §10.1 (tokens in the browser).
**Pattern source:** the private ebook library at `litlibrary.davidjdrake.com`
(repo `DavidJDrake/lit-library`), which runs invite-only Google sign-in in
production. This design copies that pattern, adapted to Terraform and Go, with
three deliberate differences named in §5.

## 1. Purpose

The admin site signs people in with an email and password on Cognito's hosted
page. The owner wants **Sign in with Google** instead, and nothing else.

The site must stay **invite-only**, and that is the hard part. Panel ownership
is decided from the signed-in user's verified email (`ownerMatches`,
`cloud/cmd/enroll/handler.go`), so whoever can obtain a token with a given
verified email can claim that person's pre-bound panels.

## 2. Facts established by investigation

1. **The pool's invite-only switch does not stop Google accounts.** AWS
   documents `AllowAdminCreateUserOnly` as governing "self-service sign-up …
   with the `SignUp` operation". Separately, on a federated user's first sign-in
   "if no linked profile exists, your user pool creates a new profile". Turning
   Google on with nothing else would give an account to anyone with a Google
   login. *This is an inference from two documentation pages, and §8 tests it
   against a real uninvited Google account rather than trusting it.*
2. **The pre sign-up trigger runs on first federated sign-in and can refuse
   it.** AWS: it fires "on … first sign-in with a trusted identity provider",
   and the function can "modify or deny the new user". The source is
   `PreSignUp_ExternalProvider`.
3. **The pre token generation trigger runs on every issuance through the hosted
   page and on refresh.** Its sources include `TokenGeneration_HostedAuth`
   and `TokenGeneration_RefreshTokens`. Event version `V1_0` is available on
   every feature plan.
4. **Google no longer lets anyone view or download an existing client secret.**
   A secret is shown once, at creation. That is why the secret lives only in
   Secrets Manager (§4.3).
5. **LitLibrary's pre sign-up trigger rejects every source except
   `PreSignUp_ExternalProvider`,** including `PreSignUp_SignUp` and
   `PreSignUp_AdminCreateUser`. Without that, anyone who knows an invited address
   could create a password account for it.

## 3. Decisions

| Question | Decision |
|---|---|
| Keep email and password as well? | No. Google is the only way in. |
| How is invite-only enforced? | An allowlist of email addresses, checked by a Lambda at account creation *and* at every token issuance |
| Where does the allowlist live? | An SSM String parameter, edited in place with `aws ssm put-parameter --overwrite` |
| Link Google to admin-created accounts? | No. Linking needs a privileged trigger, links on email (which AWS warns against), and makes a user's first sign-in fail once by design. There are no panels to migrate. |
| Where does the Google client secret live? | Secrets Manager, `scoreboard/google-oauth-client`, as `{"client_id", "client_secret"}` — LitLibrary's shape. Already created. |
| Lambda language | Go, matching every other function in this repository |

## 4. Design

### 4.1 The gate: one Lambda, two triggers

`cloud/cmd/authgate` is attached to the pool as both its pre sign-up and its pre
token generation (`V1_0`) trigger. It dispatches on `triggerSource`.

**Pre sign-up** admits a new account only when all of these hold, and refuses
otherwise:

- `triggerSource` is exactly `PreSignUp_ExternalProvider`. Native sign-up and
  admin creation are refused outright, for any address.
- The mapped `email_verified` attribute is `true` (Difference 1, §5).
- The normalized email — trimmed, then lowercased — is on the allowlist.

When it admits one, it sets `autoConfirmUser` and `autoVerifyEmail`.

**Pre token generation** refuses to issue tokens unless the account's email is
still on the allowlist (Difference 2, §5). It makes no change to any claim.

**Both fail closed.** If the allowlist cannot be read, sign-in is refused. A
broken dependency must never be the reason a stranger gets in.

**The allowlist is cached for 60 seconds** per warm instance, as in LitLibrary,
so a burst of sign-ins does not become a burst of SSM reads. Removing someone
therefore takes effect within a minute, plus the remaining life of tokens they
already hold (ID and access tokens last one hour; a refresh is re-checked).

**Refusals are logged without the address:** the trigger source, the reason,
and the email's domain. A stranger's full email address is personal data this
project has no reason to keep.

The function needs exactly one permission, `ssm:GetParameter` on the one
parameter. It needs no Cognito permissions, because it only answers yes or no.

### 4.2 The allowlist parameter

`/scoreboard/allowed-emails` is a comma-separated String parameter. Terraform
creates it once, seeded from a gitignored `terraform.tfvars` variable, with
`lifecycle { ignore_changes = [value] }`, so no later `apply` can reset invites
made from the CLI (Difference 3, §5). The value never enters this public
repository.

Invite: `aws ssm put-parameter --region us-east-1 --name /scoreboard/allowed-emails
--type String --overwrite --value "a@example.com,b@example.com"`. Remove: the same
command, without the address. The region is explicit because this machine's CLI
defaults to us-east-2, where the parameter does not exist.

### 4.3 Cognito

- `aws_cognito_identity_provider` named `Google`: client ID and secret read
  from the Secrets Manager secret, `authorize_scopes = "openid email"`, attribute
  mapping `email`, `email_verified`, and `username ← sub`.
- The pool gains `lambda_config` for both triggers, plus one
  `aws_lambda_permission` for `cognito-idp.amazonaws.com`, scoped by
  `source_arn` to this pool's ARN. The ARN already names the account, so there
  is no separate `source_account`. One grant covers both triggers, because both
  invoke the same function from the same pool.
- `allow_admin_create_user_only` stays `true` as defense in depth. If §8's
  first real Google sign-in shows it blocks federated creation, the fallback is
  LitLibrary's exact setting, with the trigger as the gate. That is a finding
  to record, not a guess to make now.
- The app client: `supported_identity_providers = ["Google"]` only;
  `explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]`, which removes
  `ALLOW_USER_SRP_AUTH`, the last password path on the client; and a
  `depends_on` on the identity provider. `write_attributes` becomes
  `["email", "email_verified"]`, because Cognito silently drops a value an
  identity provider maps to an attribute the client cannot write. Self-service
  writes to those same attributes stay closed anyway, because the client's
  `allowed_oauth_scopes` excludes `aws.cognito.signin.user.admin`, so none of
  its tokens can call `UpdateUserAttributes`.

**Terraform stores the Google client secret in state.** LitLibrary's CDK passes
it to CloudFormation by dynamic reference; Terraform has no equivalent here. The
state lives in the encrypted `hockeytrack-tfstate` bucket, which already holds
everything sensitive in this stack. Stated so a reviewer does not have to
discover it.

### 4.4 The site

- `auth.js`'s authorize URL gains `identity_provider=Google`, so Sign in goes
  straight to Google with no Cognito page in between.
- The button reads **Sign in with Google**. The "Accounts are by invitation"
  line stays.
- **A privacy policy page at `/privacy/`**, which Google requires before an
  External app can be published. It says plainly what the site keeps and for
  how long: the email address Google provides; panel names and which account
  owns which panel; a pending panel's owner hint, an unsalted SHA-256 that
  confirms a correct guess, kept a day plus DynamoDB's few days of TTL lag and
  up to 35 days of point-in-time-recovery backups; service logs kept 30 days;
  and the account's CloudTrail audit log, which records Cognito's hosted-UI
  sign-in requests with source IP and, for some, the user's `sub`, and is kept
  for a year plus 90 days of noncurrent versions (HockeyTrack's
  `terraform/cloudtrail.tf`). No advertising, no sharing, no sale.
- Its contact is the repository's GitHub issues page rather than an email
  address, so no personal address is published on a public page. The page asks
  people to leave their address out of the issue.

### 4.5 Alarm

A CloudWatch metric filter counts refusals, and an alarm notifies the existing
`security_alerts` SNS topic when refusals reach **3 or more in an hour**. A stranger
trying once is noise; repeated attempts are worth knowing about.

### 4.6 Known gaps

- **Nothing pages on a change to the gate itself.** Writes to
  `/scoreboard/allowed-emails`, to the pool's `lambda_config`, clients or
  identity providers, and to the `scoreboard-authgate` function all go
  unalerted. The sharpest case: an `UpdateUserPool` that drops the triggers
  fails open, admitting every Google account, and nobody is told. The fix is an
  EventBridge rule on those management events, sending to the security-alerts
  topic, alongside HockeyTrack's account security rules.
- **A gate that fails without logging is not counted.** A crash, throttle or
  timeout still refuses the sign-in, but writes no `sign-in refused` line, so
  `scoreboard-signin-refused` never sees it. The fix is an alarm on the
  function's Lambda `Errors` metric, which counts crashes and timeouts. A
  throttled invocation is not an error to Lambda (AWS: throttled requests
  "don't count as either `Invocations` or `Errors`"), so the same alarm needs
  `Throttles` beside it.

## 5. Differences from LitLibrary, and why

1. **Google's own `email_verified` is required.** LitLibrary admits any
   allowlisted address. Here, ownership rides on the verified email, so the
   site must not vouch for an address Google has not verified.
2. **The allowlist is re-checked at every token issuance.** LitLibrary's
   trigger gates only account creation, so removing someone there does not end
   an account they already have. Here it does, within a minute plus the
   remaining life of their current ID token. *LitLibrary very likely has this
   gap, and it deserves a ticket there.*
3. **`ignore_changes` instead of a "never edit this" comment.** Same goal:
   Terraform is structurally unable to overwrite invites.

## 6. Manual steps

**Done during design, 2026-09-13:**
- Google Cloud project `hockeytrack-scoreboard`, consent screen, and a Web
  OAuth client whose redirect URI is
  `https://scoreboard-admin-989232581535.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`.
- The client secret stored in Secrets Manager from a downloaded JSON file, and
  that file removed. A first secret was copied to the clipboard by mistake; it
  was disabled and deleted before anything used it, and the secret in use
  replaced it.
- The owner added as the one Google test user.

**Still to do, in order:**
1. Delete the email/password account created on 2026-09-13
   (`admin-delete-user`). It could not sign in once password flows are off,
   and its email would collide with the owner's Google profile.
2. `terraform apply`, then `make site`.
3. The owner signs in with Google, and §8 tests 1, 3 and 5 run.
4. Complete Google's Branding page — home page, privacy policy link, and both
   `davidjdrake.com` and `amazoncognito.com` as authorized domains (Google
   redirects back to Cognito's domain) — and **Publish app**.
5. §8 tests 2 and 4. They need the app published: while it is in Testing,
   Google itself turns away any account that is not a listed test user, so an
   uninvited account would never reach Cognito and the gate would go untested.

## 7. Out of scope

Additional identity providers; account linking; groups or roles; a UI to manage
invites (the CLI command is the interface at this size); Google brand
verification or a logo.

## 8. Testing

**Go, headless, test-first.** For the gate:

- Every `triggerSource` other than `PreSignUp_ExternalProvider` is refused,
  including for an allowlisted, verified address.
- Unverified is refused, and a missing `email_verified` is refused.
- Case and surrounding whitespace in either the address or the list do not
  matter.
- An SSM failure refuses at both triggers.
- Token generation for a removed address is refused, and for a listed one it
  returns the event unchanged.
- Refusal logs contain no local part of an address.
- Mutation checks on the source check and the verified check.

**JavaScript:** the authorize URL carries `identity_provider=Google`.

**On the real stack, and not skippable:**
1. The owner signs in with Google. Their ID token carries `email` and
   `email_verified: "true"`; the site shows their address; claiming works.
2. **An uninvited Google account is refused.** This is the only test of fact 1
   of §2, and of whether `allow_admin_create_user_only` interferes.
3. Remove the owner from the allowlist, wait a minute, sign out and sign in:
   refused. Put them back: admitted.
4. The alarm fires after three refusals in an hour.
5. `cognito-idp sign-up` against the client ID is refused (the native path is
   closed at both the client and the trigger).
