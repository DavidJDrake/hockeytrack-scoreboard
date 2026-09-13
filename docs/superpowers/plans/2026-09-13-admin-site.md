# The Admin Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A signed-in owner can claim a panel by typing the code on its screen,
download the setup file that pre-binds a new panel to them, choose the game
each panel follows, rename one, and remove one — at
`https://scoreboard.davidjdrake.com`.

**Architecture:** One static page on the CloudFront distribution that already
exists. Plain ES modules, no framework, no build step, no npm dependencies.
Sign-in is Cognito's hosted UI with the authorization-code flow and PKCE; the
ID token lives in module memory only. Four small modules hold every piece of
logic that can be tested without a browser — sign-in, the API client, the setup
file, and the view model — and `app.js` is a thin layer that wires them to the
DOM without ever turning a string into markup.

**Tech Stack:** HTML, CSS, JavaScript (ES2022 modules), Node 22's built-in test
runner (`node --test`), Terraform for the CSP, `aws s3 sync` for deployment.

**Specs:** `docs/superpowers/specs/2026-09-12-device-enrollment-design.md` §10
and §10.1 (this plan), and `docs/superpowers/specs/2026-09-07-admin-site-design.md`
§3.1, §4, §5, §5.1 and §6 (which §10 builds on).

## Global Constraints

- **Scope is exactly v1, and nothing else:** sign in, list your panels, claim a
  panel by code, download a setup file, choose a game per panel, rename a
  panel, remove a panel, sign out. No countdown clock, no "last seen", no
  brightness, no sharing, no sign-up link. Spec §10: "That is the entire
  onboarding interface and it should stay that small."
- **Tokens live in memory only.** Never `localStorage`, never
  `sessionStorage`, never a cookie this site sets. Spec §10.1.
- **Two values DO cross the sign-in redirect in `sessionStorage`, and neither is
  a token:** the PKCE verifier and the `state` value. Both are single-use,
  deleted the moment the callback reads them, and useless without the one-time
  authorization code they are bound to. A `signedIn` flag also lives there so a
  page refresh knows to go back through Cognito; it is a flag, not a credential.
- **Only the ID token is kept.** Cognito's token response also carries an
  access token and a refresh token; both are discarded unread. The refresh
  token is left to Cognito's own hosted-UI session cookie, per §10.1.
- **The ID token, not the access token, is the bearer sent to the API.**
  `ownerMatches` in `cloud/cmd/enroll/handler.go` decides who may claim a
  pre-bound panel from the `email` and `email_verified` claims, and Cognito
  access tokens carry neither. The API Gateway JWT authorizer's audience is the
  site's client ID, which is exactly an ID token's `aud`.
- **Re-authentication is a full-page redirect**, never a hidden iframe. Cognito
  returns straight away while its session cookie is valid.
- **Automatic re-authentication happens at most once a minute.** An API that
  answers 401 to a freshly issued token would otherwise redirect-loop forever.
- **Nothing turns a string into markup.** No `.innerHTML`, `.outerHTML`,
  `insertAdjacentHTML`, `document.write`, `eval` or `new Function` anywhere in
  `site/assets/`. Every element is built with `document.createElement` and text
  nodes. A test is a tripwire against an honest mistake, in every browser --
  it is not a wall, and its regex does not see every sink (a computed
  property access, `DOMParser`, `createContextualFragment`, `srcdoc`,
  `setHTMLUnsafe`). The CSP's `require-trusted-types-for 'script'` is the
  wall, and only in browsers that implement Trusted Types; `trusted-types
  'none'` stops a script from registering a permissive `default` policy that
  would hand those sinks back their old, unchecked behavior.
- **The server's error text is never shown.** Every failure maps from its HTTP
  status to a fixed message in `view.js`. A 404 on a claim reads identically
  whatever the cause, preserving the API's 404-never-403 property.
- **The site never asks for, sees, or sends a Wi-Fi password.** The setup file
  it produces carries the owner line and empty `ssid=` / `psk=` lines for the
  owner to fill in on their own computer.
- **A setup file is offered only to a verified email address.** The claim is
  refused server-side for an unverified one, so a file for it would make a
  panel nobody could claim.
- **The setup file's owner line is capped at 256 bytes, to match
  `MAX_OWNER_BYTES` in `device/scoreboard/netcfg.py`.** The panel silently
  ignores a longer one, which would leave its code claimable by any invited
  user.
- **It looks like HockeyTrack.** `site.css`, its five woff2 fonts and
  `ice.webp` are copied unchanged from `/home/jay/projects/hockeytrack/site/assets/`
  and marked as a copy in both repositories. HockeyTrack's palette is light
  (`--ice: #FFFFFF`), and the site follows the stylesheet rather than the one
  phrase in spec §10 that called it dark.
- **No npm dependencies, ever, in this site.** Nothing to install, nothing to
  audit, no supply chain. `site/package.json` exists only to mark the modules
  as ES modules for Node.
- **US spelling:** enroll, enrollment, color, license. Never enrol/enrolment.
- **This repo is PUBLIC.** `site/config.json` is generated and gitignored. Its
  values — the API URL, the Cognito domain, a public client ID — are not
  secrets, but a generated file does not belong in version control.
- **Implementers do not run `terraform plan`, `terraform apply`, `make site`,
  `make deploy`, any AWS command, or `git push`.** `terraform fmt` and
  `terraform validate` are fine. The controller runs the plan.

## The contract this site is a client for

**Hosted UI** — `https://scoreboard-admin-989232581535.auth.us-east-1.amazoncognito.com`
(`terraform output cognito_domain`, added in Task 1). App client
`terraform output user_pool_client_id`. Allowed scopes `openid email`. Callback
and logout URLs are exactly `https://scoreboard.davidjdrake.com/` and
`http://localhost:8000/` (callback only) — note the trailing slash.

**API** — `terraform output api_endpoint`, every route behind the JWT
authorizer, CORS allowing only `https://scoreboard.davidjdrake.com`:

| Call | Body | Success |
|---|---|---|
| `GET /api/devices` | — | 200 `[{"thingName","name","gameId"}]` |
| `GET /api/games` | — | 200 `{"generatedAt", "games": [{"gameId","away","home","start","state"}]}` — `start` is ISO 8601 UTC |
| `POST /api/devices/claim` | `{"code"}` | 200 `{"thingName"}`; 404 for a wrong, expired, or someone-else's code alike |
| `PUT /api/devices/{thing}/game` | `{"gameId"}` | 200 device |
| `PATCH /api/devices/{thing}` | `{"name"}` | 200 device |
| `DELETE /api/devices/{thing}` | — | 200 `{"thingName"}` |

Errors are `{"error": "<text>"}` with 400, 401, 404, 500 or 502. The text is
never displayed.

## File Structure

- **`terraform/site.tf`** (modify) — the CSP, the Cognito domain output, and
  handing `index.html` from Terraform to `make site`.
- **`site/package.json`** (new) — `"type": "module"`, no dependencies.
- **`site/assets/auth.js`** (new) — PKCE, the hosted UI, the in-memory session.
- **`site/assets/api.js`** (new) — the API client and its error kinds.
- **`site/assets/setupfile.js`** (new) — the setup file a new panel carries.
- **`site/assets/view.js`** (new) — titles, labels, choices, and every message.
- **`site/assets/app.js`** (new) — the DOM layer.
- **`site/index.html`**, **`site/assets/admin.css`** (new) — the page.
- **`site/assets/site.css`**, **`site/assets/fonts/*.woff2`**,
  **`site/assets/ice.webp`** (new, vendored from HockeyTrack).
- **`site/tests/*.test.js`**, **`site/tests/fixtures/scoreboard-setup.txt`** (new).
- **`Makefile`**, **`.github/workflows/ci.yml`**, **`.gitignore`**,
  **`docs/hardware-checks.md`** (modify).

---

### Task 1: A CSP that lets sign-in finish, and a place for the tests

**The defect this fixes, verified against the live site on 2026-09-13.** The
deployed CSP's `connect-src` names `https://cognito-idp.us-east-1.amazonaws.com`
— the user-pool API, which this site never calls — and does not name the
hosted-UI domain. The PKCE token exchange is a `fetch` POST to
`https://<domain>.auth.us-east-1.amazoncognito.com/oauth2/token`, so as
deployed, sign-in could never complete: the browser would block the one
request that turns a code into a token. The fix replaces the host rather than
adding one, so the policy gets narrower as well as correct.

**Files:**
- Modify: `terraform/site.tf`
- Create: `site/package.json`, `site/tests/csp.test.js`
- Modify: `Makefile`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `terraform output cognito_domain`; `make test-js`; the
  `site/tests/` directory every later task adds to.

- [ ] **Step 1: Create the JavaScript scaffold**

`site/package.json`:

```json
{
  "name": "scoreboard-site",
  "private": true,
  "type": "module",
  "description": "No dependencies, deliberately: plain ES modules, tested with Node's built-in runner."
}
```

In `Makefile`, add `test-js` to the `.PHONY` line, make `test` depend on it —
`test: vuln test-go test-py test-js` — and add the target after `test-py`:

```make
# The site has no dependencies and no build step, so its test runner is Node's
# own: nothing to install and nothing to audit. Node is found on PATH; under
# nvm that means running make from a shell that has loaded it.
test-js:
	@command -v node >/dev/null || { echo "node not found on PATH; the site's tests need Node 22 or later"; exit 1; }
	cd site && node --test tests/*.test.js
```

In `.github/workflows/ci.yml`, add a job before the `terraform:` job, following
the existing jobs' pinning style exactly:

```yaml
  site:
    name: Site (JavaScript)
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Check out
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      # No npm install step: the site has no dependencies, which is the point.
      - name: Set up Node
        uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
        with:
          node-version: "22"

      - name: Run tests
        run: make test-js
```

That SHA was resolved by the controller from
`gh api repos/actions/setup-node/git/ref/tags/v7.0.0`. Use it as written.

- [ ] **Step 2: Write the failing test**

`site/tests/csp.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The CSP is Terraform, but it exists for this site, so its tests live here.
const tf = readFileSync(new URL("../../terraform/site.tf", import.meta.url), "utf8");
const policy = tf.match(/content_security_policy = join\("", \[([\s\S]*?)\]\)/);

test("the CSP block can be found", () => {
  assert.ok(policy, "could not find the content_security_policy join() in site.tf");
});

const text = policy ? policy[1] : "";
const directive = (name) => (text.match(new RegExp(`${name} ([^;"]*)`)) || [])[1] || "";

test("connect-src names the hosted UI, which the token exchange posts to", () => {
  assert.match(directive("connect-src"), /aws_cognito_user_pool_domain\.admin\.domain\}\.auth\.\$\{var\.region\}\.amazoncognito\.com/);
});

test("connect-src no longer names the user-pool API the site never calls", () => {
  assert.doesNotMatch(text, /cognito-idp/);
});

test("connect-src still names the admin API", () => {
  assert.match(directive("connect-src"), /aws_apigatewayv2_api\.admin\.api_endpoint/);
});

test("scripts come from this origin only", () => {
  assert.equal(directive("script-src").trim(), "'self'");
});

test("a string can never become a script sink", () => {
  assert.match(text, /require-trusted-types-for 'script'/);
});

test("the page cannot be framed", () => {
  assert.match(text, /frame-ancestors 'none'/);
});
```

- [ ] **Step 3: Run it and watch it fail**

Run: `make test-js`
Expected: FAIL on the hosted-UI, `cognito-idp` and trusted-types tests; PASS on
the rest.

- [ ] **Step 4: Fix the CSP**

In `terraform/site.tf`, replace the `content_security_policy` value:

```hcl
    content_security_policy {
      content_security_policy = join("", [
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; ",
        "img-src 'self' data:; font-src 'self'; ",
        "connect-src 'self' ${aws_apigatewayv2_api.admin.api_endpoint} https://${aws_cognito_user_pool_domain.admin.domain}.auth.${var.region}.amazoncognito.com; ",
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; ",
        "require-trusted-types-for 'script'",
      ])
      override = true
    }
```

Rewrite the comment above that resource to say what is now true. Replace the
paragraph beginning "The CSP is deliberately wider than hockeytrack's" with:

```hcl
# The CSP is wider than hockeytrack's only where it has to be. This site signs
# users in and calls the admin API, so connect-src names exactly two hosts: the
# API, and the Cognito hosted-UI domain the PKCE token exchange posts to. An
# earlier version named cognito-idp.<region>.amazonaws.com instead -- the
# user-pool API, which this site never calls -- and so blocked the one request
# that turns an authorization code into a token. Both hosts are interpolated
# from the resources themselves rather than typed in, so the policy cannot
# drift away from what it describes.
#
# require-trusted-types-for 'script' makes assigning a string to an HTML sink
# such as innerHTML throw, in browsers that implement Trusted Types. The site
# never does that; this turns "never does" into "cannot", where supported.
# site/tests/view.test.js enforces the same rule everywhere else.
```

- [ ] **Step 5: Hand `index.html` from Terraform to `make site`, without taking the site down**

`aws_s3_object.holding_page` owns `index.html` today, and `make site` (Task 7)
will upload the real one. Two owners of one object means every
`terraform apply` would put the holding page back. Delete the
`resource "aws_s3_object" "holding_page"` block and its comment, and in its
place add:

```hcl
# index.html belonged to Terraform while the site was a holding page. It now
# belongs to `make site`, which uploads the real one. `removed` with
# destroy = false makes Terraform forget the object without deleting it, so the
# holding page stays live until the first `make site` replaces it -- rather
# than the domain serving a 404 between an apply and a deploy.
removed {
  from = aws_s3_object.holding_page

  lifecycle {
    destroy = false
  }
}
```

Update the file's header comment: remove "Nothing is built on it yet" and the
sentence about the holding page shipping with it, and say instead that the
admin page lives in `site/` and is deployed by `make site`.

- [ ] **Step 6: Output the hosted-UI domain**

Add to the outputs at the bottom of `terraform/site.tf`:

```hcl
output "cognito_domain" {
  value       = "${aws_cognito_user_pool_domain.admin.domain}.auth.${var.region}.amazoncognito.com"
  description = "Hosted-UI host the site signs in through. make site writes it into site/config.json."
}
```

- [ ] **Step 7: Verify**

Run: `make test-js` — Expected: PASS, 7 tests.
Run: `terraform -chdir=terraform fmt -check -diff && terraform -chdir=terraform validate`
Expected: both clean. Do **not** run `terraform plan`; the controller does.

- [ ] **Step 8: Commit**

```bash
git add terraform/site.tf site/package.json site/tests/csp.test.js Makefile .github/workflows/ci.yml
git commit -m "site: a CSP that lets sign-in finish"
```

---

### Task 2: Signing in

**Files:**
- Create: `site/assets/auth.js`
- Test: `site/tests/auth.test.js`

**Interfaces:**
- Produces, used by Task 6:
  - `beginSignIn(cfg, { origin, storage, navigate, cryptoImpl? }) -> Promise<void>`
  - `completeSignIn(cfg, { url, storage, fetchImpl? }) -> Promise<Session>`
  - `class Session { idToken; expiresAt; expired(now?) -> boolean; get claims }`
  - `class SignInError extends Error`
  - `logoutUrl(cfg, origin) -> string`
  - `wasSignedIn(storage) -> boolean`, `forgetSignIn(storage) -> void`
  - `mayReauth(storage, now?) -> boolean`
  - `claimsOf(jwt) -> object`
- `cfg` is `{ cognitoDomain, clientId, apiBase }`, from `/config.json`.

- [ ] **Step 1: Write the failing tests**

`site/tests/auth.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import * as auth from "../assets/auth.js";

const cfg = { cognitoDomain: "example.auth.us-east-1.amazoncognito.com", clientId: "client123" };
const ORIGIN = "https://scoreboard.example";

function memoryStorage() {
  const data = new Map();
  const writes = [];
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { writes.push([k, String(v)]); data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
    writes,
  };
}

function jwt(payload) {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${enc({ alg: "RS256" })}.${enc(payload)}.signature`;
}

function tokenEndpoint(reply, calls = []) {
  return async (url, init) => {
    calls.push({ url, init });
    return { ok: reply.ok ?? true, status: reply.status ?? 200, json: async () => reply.body };
  };
}

async function pendingSignIn() {
  const storage = memoryStorage();
  let went = null;
  await auth.beginSignIn(cfg, { origin: ORIGIN, storage, navigate: (u) => { went = u; } });
  return { storage, state: new URL(went).searchParams.get("state") };
}

test("the PKCE challenge matches RFC 7636's own worked example", async () => {
  assert.equal(await auth.challengeFor("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
    "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
});

test("a verifier is long enough for RFC 7636 and uses only unreserved characters", () => {
  const v = auth.randomString(32);
  assert.ok(v.length >= 43 && v.length <= 128, `length ${v.length}`);
  assert.match(v, /^[A-Za-z0-9_-]+$/);
});

test("two verifiers are never the same", () => {
  assert.notEqual(auth.randomString(32), auth.randomString(32));
});

test("the authorize URL asks for a code with PKCE, for exactly openid and email", () => {
  const u = new URL(auth.authorizeUrl(cfg, ORIGIN, { state: "s1", challenge: "c1" }));
  assert.equal(u.origin + u.pathname, "https://example.auth.us-east-1.amazoncognito.com/oauth2/authorize");
  const p = u.searchParams;
  assert.equal(p.get("response_type"), "code");
  assert.equal(p.get("client_id"), "client123");
  assert.equal(p.get("redirect_uri"), "https://scoreboard.example/");
  assert.equal(p.get("scope"), "openid email");
  assert.equal(p.get("code_challenge"), "c1");
  assert.equal(p.get("code_challenge_method"), "S256");
  assert.equal(p.get("state"), "s1");
});

test("beginning sign-in navigates with a challenge that matches the stored verifier", async () => {
  const storage = memoryStorage();
  let went = null;
  await auth.beginSignIn(cfg, { origin: ORIGIN, storage, navigate: (u) => { went = u; } });
  const pending = JSON.parse(storage.getItem("scoreboard.signin"));
  const p = new URL(went).searchParams;
  assert.equal(p.get("state"), pending.state);
  assert.equal(p.get("code_challenge"), await auth.challengeFor(pending.verifier));
});

test("completing sign-in exchanges the code with the stored verifier", async () => {
  const { storage, state } = await pendingSignIn();
  const verifier = JSON.parse(storage.getItem("scoreboard.signin")).verifier;
  const calls = [];
  const idToken = jwt({ email: "friend@example.com", email_verified: true });
  const session = await auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`,
    storage,
    fetchImpl: tokenEndpoint({ body: { id_token: idToken, access_token: "AT", refresh_token: "RT", expires_in: 3600 } }, calls),
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://example.auth.us-east-1.amazoncognito.com/oauth2/token");
  assert.equal(calls[0].init.method, "POST");
  const form = new URLSearchParams(calls[0].init.body);
  assert.equal(form.get("grant_type"), "authorization_code");
  assert.equal(form.get("client_id"), "client123");
  assert.equal(form.get("code"), "abc");
  assert.equal(form.get("code_verifier"), verifier);
  assert.equal(form.get("redirect_uri"), "https://scoreboard.example/");
  assert.equal(session.idToken, idToken);
});

test("a mismatched state is refused before any request is made", async () => {
  const { storage } = await pendingSignIn();
  const calls = [];
  await assert.rejects(
    auth.completeSignIn(cfg, { url: `${ORIGIN}/?code=abc&state=forged`, storage, fetchImpl: tokenEndpoint({ body: {} }, calls) }),
    auth.SignInError);
  assert.equal(calls.length, 0);
});

test("a replayed callback finds no verifier and makes no request", async () => {
  const { storage, state } = await pendingSignIn();
  const url = `${ORIGIN}/?code=abc&state=${state}`;
  await auth.completeSignIn(cfg, { url, storage, fetchImpl: tokenEndpoint({ body: { id_token: jwt({}) } }) });
  const calls = [];
  await assert.rejects(
    auth.completeSignIn(cfg, { url, storage, fetchImpl: tokenEndpoint({ body: { id_token: jwt({}) } }, calls) }),
    auth.SignInError);
  assert.equal(calls.length, 0);
});

test("the verifier is spent even when the exchange fails", async () => {
  const { storage, state } = await pendingSignIn();
  await assert.rejects(auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ ok: false, status: 400, body: {} }),
  }), auth.SignInError);
  assert.equal(storage.getItem("scoreboard.signin"), null);
});

test("a reply without an ID token is a failed sign-in", async () => {
  const { storage, state } = await pendingSignIn();
  await assert.rejects(auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ body: { access_token: "AT" } }),
  }), auth.SignInError);
});

test("no token is ever written to storage", async () => {
  const { storage, state } = await pendingSignIn();
  const idToken = jwt({ email: "friend@example.com" });
  await auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ body: { id_token: idToken, access_token: "ACCESS-SECRET", refresh_token: "REFRESH-SECRET" } }),
  });
  for (const [, value] of storage.writes) {
    assert.ok(!value.includes(idToken), "the ID token reached storage");
    assert.ok(!value.includes("ACCESS-SECRET"), "the access token reached storage");
    assert.ok(!value.includes("REFRESH-SECRET"), "the refresh token reached storage");
  }
});

test("the session keeps the ID token and nothing else from the reply", async () => {
  const { storage, state } = await pendingSignIn();
  const session = await auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ body: { id_token: jwt({}), access_token: "AT", refresh_token: "RT", expires_in: 3600 } }),
  });
  assert.deepEqual(Object.keys(session).sort(), ["expiresAt", "idToken"]);
});

test("a completed sign-in is remembered as a flag, so a refresh knows to renew", async () => {
  const { storage, state } = await pendingSignIn();
  assert.equal(auth.wasSignedIn(storage), false);
  await auth.completeSignIn(cfg, { url: `${ORIGIN}/?code=abc&state=${state}`, storage, fetchImpl: tokenEndpoint({ body: { id_token: jwt({}) } }) });
  assert.equal(auth.wasSignedIn(storage), true);
  auth.forgetSignIn(storage);
  assert.equal(auth.wasSignedIn(storage), false);
});

test("claims decode from a base64url payload, including the characters base64url changes", () => {
  const claims = auth.claimsOf(jwt({ email: "friend@example.com", note: "ü?>" }));
  assert.equal(claims.email, "friend@example.com");
  assert.equal(claims.note, "ü?>");
});

test("an undecodable token yields no claims rather than throwing", () => {
  assert.deepEqual(auth.claimsOf("not-a-jwt"), {});
});

test("a session counts as expired a minute early", () => {
  const s = new auth.Session("t", 1_000_000);
  assert.equal(s.expired(1_000_000 - 61_000), false);
  assert.equal(s.expired(1_000_000 - 59_000), true);
});

test("automatic re-authentication is refused twice inside a minute", () => {
  const storage = memoryStorage();
  assert.equal(auth.mayReauth(storage, 1_000_000), true);
  assert.equal(auth.mayReauth(storage, 1_030_000), false);
  assert.equal(auth.mayReauth(storage, 1_061_000), true);
});

test("signing out returns to this site", () => {
  const u = new URL(auth.logoutUrl(cfg, ORIGIN));
  assert.equal(u.origin + u.pathname, "https://example.auth.us-east-1.amazoncognito.com/logout");
  assert.equal(u.searchParams.get("client_id"), "client123");
  assert.equal(u.searchParams.get("logout_uri"), "https://scoreboard.example/");
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-js`
Expected: FAIL — `Cannot find module '.../site/assets/auth.js'`.

- [ ] **Step 3: Implement**

`site/assets/auth.js`:

```js
// Signing in through Cognito's hosted UI: authorization code with PKCE.
//
// The ID token lives in memory only -- never localStorage or sessionStorage --
// so it does not survive a tab close, and no script that manages to run on this
// page can read one back out of storage. Two values DO cross the redirect in
// sessionStorage, and neither is a token: the PKCE verifier and the state.
// Both are single-use, deleted the moment the callback reads them, and useless
// without the one-time authorization code they are bound to.

const PENDING_KEY = "scoreboard.signin";
const SIGNED_IN_KEY = "scoreboard.signedIn"; // a flag, not a credential
const REAUTH_KEY = "scoreboard.reauthAt";
const REAUTH_INTERVAL_MS = 60_000;
const EXPIRY_MARGIN_MS = 60_000;

export class SignInError extends Error {}

export function base64url(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomString(byteLength = 32, cryptoImpl = globalThis.crypto) {
  const bytes = new Uint8Array(byteLength);
  cryptoImpl.getRandomValues(bytes);
  return base64url(bytes);
}

export async function challengeFor(verifier, cryptoImpl = globalThis.crypto) {
  const digest = await cryptoImpl.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function hostedUi(cfg, path) {
  return new URL(path, `https://${cfg.cognitoDomain}`);
}

// Cognito matches this against the app client's callback URLs exactly,
// trailing slash included.
function redirectUri(origin) {
  return `${origin}/`;
}

export function authorizeUrl(cfg, origin, { state, challenge }) {
  const url = hostedUi(cfg, "/oauth2/authorize");
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: cfg.clientId,
    redirect_uri: redirectUri(origin),
    scope: "openid email",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  }).toString();
  return url.toString();
}

export function logoutUrl(cfg, origin) {
  const url = hostedUi(cfg, "/logout");
  url.search = new URLSearchParams({ client_id: cfg.clientId, logout_uri: redirectUri(origin) }).toString();
  return url.toString();
}

export async function beginSignIn(cfg, { origin, storage, navigate, cryptoImpl = globalThis.crypto }) {
  const verifier = randomString(32, cryptoImpl);
  const state = randomString(16, cryptoImpl);
  storage.setItem(PENDING_KEY, JSON.stringify({ verifier, state }));
  navigate(authorizeUrl(cfg, origin, { state, challenge: await challengeFor(verifier, cryptoImpl) }));
}

export async function completeSignIn(cfg, { url, storage, fetchImpl = globalThis.fetch }) {
  const here = new URL(url);
  const code = here.searchParams.get("code");
  const state = here.searchParams.get("state");
  // Read and delete in one motion, before anything can fail: the verifier is
  // single-use, and a replayed callback must find nothing to use.
  const raw = storage.getItem(PENDING_KEY);
  storage.removeItem(PENDING_KEY);
  let pending = null;
  try {
    pending = raw ? JSON.parse(raw) : null;
  } catch {
    pending = null;
  }
  if (!code || !state || !pending || pending.state !== state) {
    throw new SignInError("sign-in could not be completed");
  }
  const resp = await fetchImpl(hostedUi(cfg, "/oauth2/token").toString(), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: cfg.clientId,
      code,
      redirect_uri: redirectUri(here.origin),
      code_verifier: pending.verifier,
    }).toString(),
  });
  if (!resp.ok) throw new SignInError("sign-in could not be completed");
  const body = await resp.json();
  if (!body || typeof body.id_token !== "string") throw new SignInError("sign-in could not be completed");
  // The access and refresh tokens in `body` are deliberately not kept. The
  // API is called with the ID token, and renewal is Cognito's own session
  // cookie, reached by sending the browser back through the hosted UI.
  storage.setItem(SIGNED_IN_KEY, "1");
  const lifetimeMs = (Number(body.expires_in) || 3600) * 1000;
  return new Session(body.id_token, Date.now() + lifetimeMs);
}

export class Session {
  constructor(idToken, expiresAt) {
    this.idToken = idToken;
    this.expiresAt = expiresAt;
  }

  expired(now = Date.now()) {
    return now >= this.expiresAt - EXPIRY_MARGIN_MS;
  }

  get claims() {
    return claimsOf(this.idToken);
  }
}

// Decodes, and does NOT verify. That is fine for what this page uses it for --
// showing the signed-in address and writing it into a setup file -- because
// the token came straight from Cognito's token endpoint over TLS, and the API
// verifies it properly on every call. Never use these claims to decide what
// the user may do.
export function claimsOf(jwt) {
  const part = String(jwt).split(".")[1] ?? "";
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
  try {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes));
    return claims && typeof claims === "object" ? claims : {};
  } catch {
    return {};
  }
}

export function wasSignedIn(storage) {
  return storage.getItem(SIGNED_IN_KEY) === "1";
}

export function forgetSignIn(storage) {
  storage.removeItem(SIGNED_IN_KEY);
  storage.removeItem(PENDING_KEY);
}

// An API that answers 401 to a freshly issued token would otherwise send the
// browser round the hosted UI forever -- which also looks, in the logs, like an
// attack. Once a minute is plenty for a real expiry.
export function mayReauth(storage, now = Date.now()) {
  const last = Number(storage.getItem(REAUTH_KEY)) || 0;
  if (now - last < REAUTH_INTERVAL_MS) return false;
  storage.setItem(REAUTH_KEY, String(now));
  return true;
}
```

- [ ] **Step 4: Run the tests**

Run: `make test-js`
Expected: PASS — 7 CSP tests and 18 auth tests.

- [ ] **Step 5: Prove two of these tests are real**

```bash
cd site
cp assets/auth.js /tmp/auth.bak
# 1. Stop deleting the verifier before the exchange. The same line also
#    appears in forgetSignIn, so `0,/.../` limits the edit to the FIRST one,
#    which is completeSignIn's. Confirm with the grep that exactly one
#    occurrence changed.
sed -i '0,/^  storage.removeItem(PENDING_KEY);$/s//  \/\/ storage.removeItem(PENDING_KEY);/' assets/auth.js
grep -c '^  // storage.removeItem(PENDING_KEY);$' assets/auth.js   # expect: 1
node --test tests/auth.test.js 2>&1 | grep -E "^# (pass|fail)"
# expect: fail >= 2 (the replay test and the spent-verifier test)
cp /tmp/auth.bak assets/auth.js
# 2. Stop checking the state.
sed -i 's/ || pending.state !== state)/)/' assets/auth.js
node --test tests/auth.test.js 2>&1 | grep -E "^# (pass|fail)"
# expect: fail >= 1 (the mismatched-state test)
cp /tmp/auth.bak assets/auth.js && rm /tmp/auth.bak
git diff --stat assets/auth.js   # expect: no output
```

Report both results. If either mutation leaves every test passing, the test is
not testing what its name says — fix the test.

- [ ] **Step 6: Commit**

```bash
git add site/assets/auth.js site/tests/auth.test.js
git commit -m "site: sign in with PKCE, and keep the token in memory"
```

---

### Task 3: The API client

**Files:**
- Create: `site/assets/api.js`
- Test: `site/tests/api.test.js`

**Interfaces:**
- Produces, used by Task 6:
  - `createApi({ base, getToken, onUnauthorized, fetchImpl? })` returning
    `{ listDevices, listGames, claim(code), setGame(thing, gameId), rename(thing, name), unbind(thing) }`,
    each `-> Promise<parsed JSON>`
  - `class ApiError extends Error { kind; status }` where `kind` is one of
    `"unauthorized" | "not-found" | "bad-request" | "unavailable" | "failed"`

- [ ] **Step 1: Write the failing tests**

`site/tests/api.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, createApi } from "../assets/api.js";

function harness(reply, { token = "ID-TOKEN" } = {}) {
  const calls = [];
  const state = { unauthorized: 0 };
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (reply instanceof Error) throw reply;
    return { ok: reply.status >= 200 && reply.status < 300, status: reply.status, json: async () => reply.body };
  };
  const api = createApi({
    base: "https://api.example/",
    getToken: () => token,
    onUnauthorized: () => { state.unauthorized += 1; },
    fetchImpl,
  });
  return { api, calls, state };
}

async function rejectionOf(promise) {
  try {
    await promise;
  } catch (err) {
    return err;
  }
  assert.fail("expected the call to be rejected");
}

test("every call carries the ID token as a bearer", async () => {
  const { api, calls } = harness({ status: 200, body: [] });
  await api.listDevices();
  assert.equal(calls[0].init.headers.authorization, "Bearer ID-TOKEN");
});

test("each call uses the documented method, path and body", async () => {
  const cases = [
    [(api) => api.listDevices(), "GET", "/api/devices", undefined],
    [(api) => api.listGames(), "GET", "/api/games", undefined],
    [(api) => api.claim("  7K4M-9QX2 "), "POST", "/api/devices/claim", { code: "7K4M-9QX2" }],
    [(api) => api.setGame("t1", 2025020001), "PUT", "/api/devices/t1/game", { gameId: 2025020001 }],
    [(api) => api.rename("t1", " Den "), "PATCH", "/api/devices/t1", { name: "Den" }],
    [(api) => api.unbind("t1"), "DELETE", "/api/devices/t1", undefined],
  ];
  for (const [call, method, path, body] of cases) {
    const { api, calls } = harness({ status: 200, body: {} });
    await call(api);
    assert.equal(calls[0].init.method, method, path);
    assert.equal(calls[0].url, `https://api.example${path}`);
    if (body === undefined) {
      assert.equal(calls[0].init.body, undefined, `${method} ${path} sent a body`);
    } else {
      assert.deepEqual(JSON.parse(calls[0].init.body), body);
      assert.equal(calls[0].init.headers["content-type"], "application/json");
    }
  }
});

test("a thing name is encoded into the path", async () => {
  const { api, calls } = harness({ status: 200, body: {} });
  await api.unbind("a/b?c");
  assert.equal(calls[0].url, "https://api.example/api/devices/a%2Fb%3Fc");
});

test("a 404 is a not-found error", async () => {
  const { api } = harness({ status: 404, body: { error: "no enrollment with that code" } });
  const err = await rejectionOf(api.claim("X"));
  assert.ok(err instanceof ApiError);
  assert.equal(err.kind, "not-found");
});

test("a 400 is a bad request and a 502 is unavailable", async () => {
  assert.equal((await rejectionOf(harness({ status: 400, body: {} }).api.claim("X"))).kind, "bad-request");
  assert.equal((await rejectionOf(harness({ status: 502, body: {} }).api.listGames())).kind, "unavailable");
});

test("a 401 asks to sign in again", async () => {
  const { api, state } = harness({ status: 401, body: {} });
  const err = await rejectionOf(api.listDevices());
  assert.equal(err.kind, "unauthorized");
  assert.equal(state.unauthorized, 1);
});

test("no request is made without a token", async () => {
  const { api, calls, state } = harness({ status: 200, body: [] }, { token: null });
  const err = await rejectionOf(api.listDevices());
  assert.equal(err.kind, "unauthorized");
  assert.equal(calls.length, 0);
  assert.equal(state.unauthorized, 1);
});

test("the server's own error text never reaches the error", async () => {
  const { api } = harness({ status: 500, body: { error: "<img src=x onerror=alert(1)>" } });
  const err = await rejectionOf(api.listDevices());
  assert.equal(err.kind, "failed");
  assert.ok(!err.message.includes("img"), err.message);
});

test("a network failure is unavailable", async () => {
  const { api } = harness(new TypeError("Failed to fetch"));
  assert.equal((await rejectionOf(api.listDevices())).kind, "unavailable");
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-js` — Expected: FAIL, `Cannot find module '.../site/assets/api.js'`.

- [ ] **Step 3: Implement**

`site/assets/api.js`:

```js
// The admin API, called with the signed-in user's ID token.
//
// A failure becomes an ApiError whose `kind` is derived from the HTTP status
// alone. The server's own error text is never carried: the page shows a fixed
// message per kind (view.js), so nothing the server says can reach the screen,
// and a 404 on a claim reads the same whatever caused it.

export class ApiError extends Error {
  constructor(kind, status) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

function kindFor(status) {
  if (status === 401) return "unauthorized";
  if (status === 404) return "not-found";
  if (status === 400) return "bad-request";
  if (status === 502 || status === 503 || status === 504) return "unavailable";
  return "failed";
}

export function createApi({ base, getToken, onUnauthorized, fetchImpl = globalThis.fetch }) {
  const root = String(base).replace(/\/+$/, "");

  async function call(method, path, body) {
    const token = getToken();
    if (!token) {
      onUnauthorized();
      throw new ApiError("unauthorized", 0);
    }
    const headers = { authorization: `Bearer ${token}` };
    const init = { method, headers };
    if (body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetchImpl(root + path, init);
    } catch {
      throw new ApiError("unavailable", 0);
    }
    if (resp.status === 401) {
      onUnauthorized();
      throw new ApiError("unauthorized", 401);
    }
    if (!resp.ok) throw new ApiError(kindFor(resp.status), resp.status);
    return resp.json();
  }

  // Thing names are assigned by the server, but a path segment is still built
  // from data, so it is encoded rather than trusted.
  const device = (thing) => `/api/devices/${encodeURIComponent(thing)}`;

  return {
    listDevices: () => call("GET", "/api/devices"),
    listGames: () => call("GET", "/api/games"),
    claim: (code) => call("POST", "/api/devices/claim", { code: String(code).trim() }),
    setGame: (thing, gameId) => call("PUT", `${device(thing)}/game`, { gameId }),
    rename: (thing, name) => call("PATCH", device(thing), { name: String(name).trim() }),
    unbind: (thing) => call("DELETE", device(thing)),
  };
}
```

- [ ] **Step 4: Run the tests**

Run: `make test-js` — Expected: PASS, 9 new API tests.

- [ ] **Step 5: Commit**

```bash
git add site/assets/api.js site/tests/api.test.js
git commit -m "site: an API client that never shows the server's words"
```

---

### Task 4: The setup file, pinned from both sides

The website writes this file and the panel reads it, in two different
languages that have never met. One committed fixture is asserted from both
sides — the JavaScript test says the site produces exactly it, and a Python
test says the panel reads it as intended — so the contract is checked in CI
rather than discovered on a stranger's SD card.

**Files:**
- Create: `site/assets/setupfile.js`, `site/tests/fixtures/scoreboard-setup.txt`
- Test: `site/tests/setupfile.test.js`
- Modify: `device/tests/test_netcfg.py`

**Interfaces:**
- Produces, used by Task 6: `setupFileFor(email) -> string`,
  `SETUP_FILE_NAME` (`"scoreboard-setup.txt"`), `MAX_OWNER_BYTES` (256),
  `class SetupFileError extends Error`.

- [ ] **Step 1: Write the fixture**

`site/tests/fixtures/scoreboard-setup.txt` — exactly this, ending in a single
newline:

```
# HockeyTrack scoreboard setup
#
# Save this file onto your panel's SD card, in the partition your computer
# can open, named exactly: scoreboard-setup.txt
#
# Type your Wi-Fi network name and password after the = signs below. The
# panel reads them when it starts, connects, and then removes the password
# from this file.
ssid=
psk=

# This line ties the panel to your account, so the code it shows can be
# claimed by you and nobody else. Leave it exactly as it is.
owner=friend@example.com
```

- [ ] **Step 2: Write the failing tests**

`site/tests/setupfile.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MAX_OWNER_BYTES, SETUP_FILE_NAME, SetupFileError, setupFileFor } from "../assets/setupfile.js";

const fixture = readFileSync(new URL("./fixtures/scoreboard-setup.txt", import.meta.url), "utf8");

test("the generated file is exactly the fixture the panel's tests read", () => {
  assert.equal(setupFileFor("friend@example.com"), fixture);
});

test("the file is named what the panel looks for", () => {
  assert.equal(SETUP_FILE_NAME, "scoreboard-setup.txt");
});

test("the file carries no Wi-Fi details, only empty lines to fill in", () => {
  const text = setupFileFor("friend@example.com");
  assert.match(text, /^ssid=$/m);
  assert.match(text, /^psk=$/m);
});

test("a line break in the address is refused, so it cannot add lines of its own", () => {
  assert.throws(() => setupFileFor("a@example.com\nssid=evil"), SetupFileError);
  assert.throws(() => setupFileFor("a@example.com\rpsk=evil"), SetupFileError);
});

test("an empty address is refused", () => {
  assert.throws(() => setupFileFor(""), SetupFileError);
  assert.throws(() => setupFileFor(undefined), SetupFileError);
});

test("an address the panel would ignore is refused", () => {
  assert.throws(() => setupFileFor("a".repeat(250) + "@ex.com"), SetupFileError);
});

test("the cap counts bytes, not characters", () => {
  // é is two bytes. 127 of them plus "@x" is exactly 256 bytes; 128 is 258,
  // though only 130 characters -- which a character count would wave through.
  assert.doesNotThrow(() => setupFileFor("é".repeat(127) + "@x"));
  assert.throws(() => setupFileFor("é".repeat(128) + "@x"), SetupFileError);
});

test("the cap agrees with the panel's own", () => {
  const netcfg = readFileSync(new URL("../../device/scoreboard/netcfg.py", import.meta.url), "utf8");
  const found = netcfg.match(/^MAX_OWNER_BYTES = (\d+)$/m);
  assert.ok(found, "MAX_OWNER_BYTES not found in device/scoreboard/netcfg.py");
  assert.equal(Number(found[1]), MAX_OWNER_BYTES);
});
```

Add to `device/tests/test_netcfg.py`, with `from pathlib import Path` added to
its imports if absent:

```python
SITE_SETUP_FIXTURE = (Path(__file__).resolve().parents[2]
                      / "site" / "tests" / "fixtures" / "scoreboard-setup.txt")


def test_the_setup_file_the_website_writes_is_read_the_way_it_meant():
    # One fixture, asserted from both sides: site/tests/setupfile.test.js
    # checks the website produces exactly this text, and this checks the panel
    # reads it as the website intended. Neither side can drift alone.
    text = SITE_SETUP_FIXTURE.read_text(encoding="utf-8")
    assert netcfg.parse_owner(text) == "friend@example.com"
    # Downloaded and never edited, it must leave the network alone rather than
    # fail -- ssid= with nothing after it is "nothing to do".
    assert netcfg.parse_wifi_file(text) is None
```

- [ ] **Step 3: Run them and watch them fail**

Run: `make test-js` — Expected: FAIL, `Cannot find module '.../site/assets/setupfile.js'`.
Run: `make test-py` — Expected: PASS already, because the fixture exists and the
panel's parser is already correct. That is the point of the fixture; note it.

- [ ] **Step 4: Implement**

`site/assets/setupfile.js`:

```js
// The file a new panel carries on its boot partition.
//
// It holds the owner line, which is what makes the panel's pairing code
// claimable by this person alone, and empty Wi-Fi lines for them to fill in on
// their own computer. The site never asks for a Wi-Fi password, never sees one,
// and never sends one anywhere.

export const SETUP_FILE_NAME = "scoreboard-setup.txt";

// Must match MAX_OWNER_BYTES in device/scoreboard/netcfg.py, and a test checks
// that it does. The panel ignores a longer owner line, which would leave its
// code claimable by any invited user -- so refusing here is what keeps the two
// halves honest with each other.
export const MAX_OWNER_BYTES = 256;

export class SetupFileError extends Error {}

export function setupFileFor(email) {
  const owner = typeof email === "string" ? email : "";
  if (!owner) throw new SetupFileError("there is no email address to write");
  // A line break would let the address add lines of its own to the file.
  if (/[\r\n]/.test(owner)) throw new SetupFileError("an email address cannot contain a line break");
  if (new TextEncoder().encode(owner).length > MAX_OWNER_BYTES) {
    throw new SetupFileError("that email address is too long for a panel to read");
  }
  return [
    "# HockeyTrack scoreboard setup",
    "#",
    "# Save this file onto your panel's SD card, in the partition your computer",
    "# can open, named exactly: scoreboard-setup.txt",
    "#",
    "# Type your Wi-Fi network name and password after the = signs below. The",
    "# panel reads them when it starts, connects, and then removes the password",
    "# from this file.",
    "ssid=",
    "psk=",
    "",
    "# This line ties the panel to your account, so the code it shows can be",
    "# claimed by you and nobody else. Leave it exactly as it is.",
    `owner=${owner}`,
    "",
  ].join("\n");
}
```

- [ ] **Step 5: Run the tests**

Run: `make test-js` — Expected: PASS, 8 new setup-file tests.
Run: `make test-py` — Expected: PASS, including the new fixture test.

- [ ] **Step 6: Prove the fixture test bites from the Python side**

```bash
cp site/tests/fixtures/scoreboard-setup.txt /tmp/fixture.bak
sed -i 's/^owner=friend@example.com$/owner=someone-else@example.com/' site/tests/fixtures/scoreboard-setup.txt
make test-py 2>&1 | tail -3     # expect: the new Python test FAILS
make test-js 2>&1 | grep -E "^# (pass|fail)"   # expect: the JS equality test FAILS too
cp /tmp/fixture.bak site/tests/fixtures/scoreboard-setup.txt && rm /tmp/fixture.bak
git status --porcelain site/tests/fixtures/   # expect: only the new, untracked fixture
```

Report both results.

- [ ] **Step 7: Commit**

```bash
git add site/assets/setupfile.js site/tests/setupfile.test.js site/tests/fixtures/scoreboard-setup.txt device/tests/test_netcfg.py
git commit -m "site: the setup file, pinned by one fixture from both languages"
```

---

### Task 5: It looks like HockeyTrack

**Files:**
- Create (vendored): `site/assets/site.css`, `site/assets/ice.webp`,
  `site/assets/fonts/{barlow-condensed-600,barlow-condensed-700,plex-mono-400,plex-mono-500,plex-sans}.woff2`
- Create: `site/index.html`, `site/assets/admin.css`, `site/assets/view.js`
- Test: `site/tests/view.test.js`

**Interfaces:**
- Produces, used by Task 6:
  - `panelTitle(device) -> string`
  - `gameLabel(game, { timeZone?, locale? }) -> string`
  - `gameChoices(device, games, options?) -> Array<{ value, label, selected, disabled }>`
  - `emailVerified(claims) -> boolean`
  - `messageFor(action, kind) -> string`, where `action` is one of
    `"claim" | "setGame" | "rename" | "unbind" | "list" | "games" | "any"`
  - Element IDs in `index.html` that Task 6 wires: `who`, `sign-out`,
    `status`, `signed-out`, `sign-in`, `signed-in`, `panels`, `claim`,
    `claim-code`, `add`, `add-note`, `download`.

- [ ] **Step 1: Vendor HockeyTrack's assets**

```bash
SRC=/home/jay/projects/hockeytrack/site/assets
mkdir -p site/assets/fonts
cp "$SRC/site.css" "$SRC/ice.webp" site/assets/
cp "$SRC"/fonts/*.woff2 site/assets/fonts/
ls site/assets/fonts/   # expect exactly the five woff2 files
cmp "$SRC/site.css" site/assets/site.css && echo "site.css identical"
```

`site.css` references `/assets/fonts/*.woff2` and `/assets/ice.webp` by
absolute path, and this site serves them at the same paths, so the stylesheet
is used **unchanged** — do not edit it. The copy is marked as a copy by Task 7,
in both repositories.

- [ ] **Step 2: Write the failing tests**

`site/tests/view.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { emailVerified, gameChoices, gameLabel, messageFor, panelTitle } from "../assets/view.js";

const games = [
  { gameId: 1, away: "TOR", home: "MTL", start: "2026-10-08T23:00:00Z", state: "FUT" },
  { gameId: 2, away: "BOS", home: "NYR", start: "2026-10-09T00:00:00Z", state: "FUT" },
];

test("a panel is titled by its name, falling back to its thing name", () => {
  assert.equal(panelTitle({ thingName: "scoreboard-abc", name: "Den" }), "Den");
  assert.equal(panelTitle({ thingName: "scoreboard-abc", name: "   " }), "scoreboard-abc");
  assert.equal(panelTitle({ thingName: "scoreboard-abc" }), "scoreboard-abc");
});

test("a game is labelled away at home, with a local start time", () => {
  const label = gameLabel(games[0], { timeZone: "America/New_York", locale: "en-US" });
  // \s, because newer ICU puts a narrow no-break space before "PM".
  assert.match(label, /^TOR at MTL · 7:00\sPM$/);
});

test("a game with an unreadable start time still shows its teams", () => {
  assert.equal(gameLabel({ ...games[0], start: "soon" }), "TOR at MTL");
});

test("a panel set to one of today's games has it selected", () => {
  const choices = gameChoices({ gameId: 2 }, games, { timeZone: "UTC" });
  assert.equal(choices.length, 2);
  assert.deepEqual(choices.map((c) => c.selected), [false, true]);
  assert.deepEqual(choices.map((c) => c.value), ["1", "2"]);
});

test("a panel not set for today gets a disabled placeholder first", () => {
  const choices = gameChoices({ gameId: 999 }, games, { timeZone: "UTC" });
  assert.equal(choices.length, 3);
  assert.deepEqual(choices[0], { value: "", label: "Not set for today", selected: true, disabled: true });
  assert.ok(choices.slice(1).every((c) => !c.selected && !c.disabled));
});

test("an email counts as verified only when the token says so", () => {
  assert.equal(emailVerified({ email_verified: true }), true);
  assert.equal(emailVerified({ email_verified: "true" }), true);
  assert.equal(emailVerified({ email_verified: false }), false);
  assert.equal(emailVerified({ email_verified: "false" }), false);
  assert.equal(emailVerified({}), false);
  assert.equal(emailVerified(undefined), false);
});

test("a wrong code gets a message about the code, and nothing about why", () => {
  const text = messageFor("claim", "not-found");
  assert.match(text, /code/i);
});

test("an unknown action or kind falls back to the general failure", () => {
  assert.equal(messageFor("rename", "teapot"), messageFor("any", "failed"));
  assert.equal(messageFor("nonsense", "failed"), messageFor("any", "failed"));
});

test("every message is plain text", () => {
  for (const action of ["claim", "setGame", "rename", "unbind", "list", "games", "any"]) {
    for (const kind of ["unauthorized", "not-found", "bad-request", "unavailable", "failed"]) {
      const text = messageFor(action, kind);
      assert.equal(typeof text, "string");
      assert.ok(text.length > 0);
      assert.doesNotMatch(text, /[<>]/, `${action}/${kind} looks like markup`);
    }
  }
});

test("no script on this site turns a string into markup", () => {
  const dir = new URL("../assets/", import.meta.url);
  const scripts = readdirSync(dir).filter((name) => name.endsWith(".js"));
  // Guards against this test silently scanning an empty or wrong directory.
  assert.ok(scripts.includes("view.js") && scripts.includes("auth.js"), `scanned ${scripts.join(", ")}`);
  const sinks = /\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function\s*\(/;
  for (const name of scripts) {
    const source = readFileSync(new URL(name, dir), "utf8");
    assert.doesNotMatch(source, sinks, `${name} uses an HTML or code sink`);
  }
});
```

Note for whoever writes `app.js` in Task 6: that last test scans every `.js`
file in `site/assets/`, so a *comment* containing `.innerHTML` would fail it
too. Write "never innerHTML" without the leading dot.

- [ ] **Step 3: Run them and watch them fail**

Run: `make test-js` — Expected: FAIL, `Cannot find module '.../site/assets/view.js'`.

- [ ] **Step 4: Implement `view.js`**

`site/assets/view.js`:

```js
// What the page says. Every sentence a user reads is here, keyed by what they
// were doing and what kind of failure came back -- never built from anything
// the server sent.

export function panelTitle(device) {
  const name = String(device?.name ?? "").trim();
  return name || device.thingName;
}

export function gameLabel(game, { timeZone, locale } = {}) {
  const teams = `${game.away} at ${game.home}`;
  const when = new Date(game.start);
  if (Number.isNaN(when.getTime())) return teams;
  const time = new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit", timeZone }).format(when);
  return `${teams} · ${time}`;
}

export function gameChoices(device, games, options = {}) {
  const choices = games.map((game) => ({
    value: String(game.gameId),
    label: gameLabel(game, options),
    selected: game.gameId === device.gameId,
    disabled: false,
  }));
  if (!choices.some((choice) => choice.selected)) {
    choices.unshift({ value: "", label: "Not set for today", selected: true, disabled: true });
  }
  return choices;
}

// The ID token carries a real boolean; API Gateway's authorizer flattens it to
// a string. Accept both, and nothing else.
export function emailVerified(claims) {
  return claims?.email_verified === true || claims?.email_verified === "true";
}

const MESSAGES = {
  claim: {
    // Identical whether the code is wrong, expired, already claimed, or was
    // set up for somebody else: the API does not say which, and neither does
    // this.
    "not-found": "No panel is waiting for you with that code. Check it against the screen — codes change every 15 minutes, and a panel set up for someone else can only be claimed by them.",
    "bad-request": "Type the code shown on your panel's screen.",
  },
  setGame: { "not-found": "That panel is no longer on your account." },
  rename: {
    "not-found": "That panel is no longer on your account.",
    "bad-request": "A panel needs a name.",
  },
  unbind: { "not-found": "That panel is no longer on your account." },
  list: {},
  games: {
    unavailable: "Today's games could not be loaded, so a game cannot be chosen right now.",
    failed: "Today's games could not be loaded, so a game cannot be chosen right now.",
  },
  any: {
    unauthorized: "Your session ended. Signing you in again…",
    unavailable: "The scoreboard service could not be reached. Try again in a moment.",
    failed: "Something went wrong on our side. Try again in a moment.",
  },
};

export function messageFor(action, kind) {
  return MESSAGES[action]?.[kind] ?? MESSAGES.any[kind] ?? MESSAGES.any.failed;
}
```

- [ ] **Step 5: Write the page**

`site/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="description" content="Add a HockeyTrack scoreboard panel to your account and choose the game it follows.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%92%3C/text%3E%3C/svg%3E">
<link rel="preload" href="/assets/fonts/plex-sans.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/assets/fonts/barlow-condensed-700.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/assets/site.css">
<link rel="stylesheet" href="/assets/admin.css">
<title>Scoreboards · HockeyTrack</title>
<script type="module" src="/assets/app.js"></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<nav class="nav" aria-label="Site">
  <div class="wrap">
    <a class="wordmark" href="/">HOCKEYTRACK</a>
    <ul>
      <li><span id="who" class="who" hidden></span></li>
      <li><button id="sign-out" class="link" type="button" hidden>Sign out</button></li>
    </ul>
  </div>
</nav>

<main class="wrap" id="main">
  <h1>Scoreboards</h1>
  <p class="status" id="status" role="status" aria-live="polite"></p>
  <noscript><p class="status error">This page needs JavaScript to sign you in.</p></noscript>

  <section id="signed-out" hidden>
    <p class="lede">Sign in to add a scoreboard panel to your account, choose the game it follows, or remove it. Accounts are by invitation.</p>
    <button id="sign-in" class="btn primary" type="button">Sign in</button>
  </section>

  <section id="signed-in" hidden>
    <ul class="panels" id="panels" aria-label="Your panels" hidden></ul>

    <form class="card" id="claim" novalidate>
      <h2>Claim a panel</h2>
      <p>Type the code shown on the panel's screen.</p>
      <div class="row">
        <label for="claim-code" class="sr-only">Pairing code</label>
        <input id="claim-code" class="code" type="text" name="code" autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="16">
        <button class="btn primary" type="submit">Claim</button>
      </div>
    </form>

    <section class="card" id="add" aria-labelledby="add-heading">
      <h2 id="add-heading">Set up a new panel</h2>
      <ol>
        <li>Prepare an SD card with the scoreboard installed.</li>
        <li>Download the setup file and save it onto the card's boot partition.</li>
        <li>Open it there, and type your Wi-Fi name and password.</li>
        <li>Put the card in the panel and power it on. When it shows a code, claim it above.</li>
      </ol>
      <p id="add-note" hidden></p>
      <button id="download" class="btn" type="button">Download setup file</button>
    </section>
  </section>
</main>

<footer class="site-foot">
  <div class="wrap">
    <p>Part of <a href="https://hockeytrack.davidjdrake.com/">HockeyTrack</a>.</p>
  </div>
</footer>
</body>
</html>
```

`site/assets/admin.css`:

```css
/* This site's own styles. The house style -- palette, type, the nav and the
   footer -- is site.css, vendored unchanged from HockeyTrack. What is here
   follows the patterns HockeyTrack's own pages already use: the uppercase
   condensed button, and the panel with a 2px border. */

h1 { font-family: var(--display); font-weight: 700; font-size: clamp(36px, 5vw, 52px); line-height: 1; margin: 40px 0 12px; }
.lede { color: var(--muted); max-width: 60ch; margin: 0 0 24px; }
.status { min-height: 1.5em; margin: 0 0 20px; font-weight: 500; }
.status.error { color: var(--red); }
.who { font-family: var(--mono); font-size: 13px; color: var(--muted); }

.btn { display: inline-block; font: inherit; font-family: var(--display); font-weight: 600; font-size: 19px; letter-spacing: 0.04em; text-transform: uppercase; text-decoration: none; padding: 10px 18px; border: 2px solid var(--blue); color: var(--blue); background: var(--ice); cursor: pointer; }
.btn:hover { background: var(--blue); color: #fff; }
.btn.primary { background: var(--blue); color: #fff; }
.btn.primary:hover { background: var(--red); border-color: var(--red); }
.btn.quiet { border-color: var(--rule); color: var(--muted); }
.btn.quiet:hover { background: var(--red); border-color: var(--red); color: #fff; }
.btn[disabled], .btn[disabled]:hover { opacity: 0.5; cursor: not-allowed; background: var(--ice); color: var(--blue); border-color: var(--blue); }
.link { font: inherit; background: none; border: 0; padding: 0; color: var(--blue); cursor: pointer; text-decoration: underline; }

.panels { display: grid; gap: 16px; margin: 0 0 32px; padding: 0; list-style: none; }
.panel { border: 2px solid var(--ink); background: var(--ice); padding: 18px 20px; display: grid; gap: 12px; }
.panel h2 { font-family: var(--display); font-weight: 700; font-size: 28px; line-height: 1.05; margin: 0; overflow-wrap: anywhere; }
.panel .thing { font-family: var(--mono); font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.row label { font-weight: 600; font-size: 13.5px; }
.row input[type="text"], .row select { font: inherit; padding: 8px 10px; border: 2px solid var(--rule); background: var(--ice); color: var(--ink); min-width: 0; max-width: 100%; }
.row input[type="text"]:focus-visible, .row select:focus-visible { border-color: var(--blue); }
.code { font-family: var(--mono); font-size: 22px; letter-spacing: 0.08em; text-transform: uppercase; width: 12ch; }

.card { border: 2px solid var(--blue); background: var(--blue-soft); padding: 18px 20px; margin: 0 0 32px; }
.card h2 { font-family: var(--display); font-weight: 700; font-size: 24px; margin: 0 0 8px; }
.card p { margin: 0 0 12px; }
.card ol { margin: 8px 0 16px; padding-left: 20px; }
```

- [ ] **Step 6: Run the tests**

Run: `make test-js` — Expected: PASS, 10 new view tests.

- [ ] **Step 7: Commit**

```bash
git add site/index.html site/assets/admin.css site/assets/view.js site/tests/view.test.js \
        site/assets/site.css site/assets/ice.webp site/assets/fonts/
git commit -m "site: the page, in HockeyTrack's own clothes"
```

---

### Task 6: The page works

**Files:**
- Create: `site/assets/app.js`

**Interfaces:**
- Consumes everything above: `auth.js` (Task 2), `api.js` (Task 3),
  `setupfile.js` (Task 4), `view.js` and the element IDs in `index.html`
  (Task 5).
- Reads `/config.json`: `{ "apiBase", "cognitoDomain", "clientId" }`, written
  by `make site-config` in Task 7.

**How this task is verified.** `app.js` is the DOM layer, and there is no DOM in
Node. Its automated coverage is the modules it composes — every one tested in
Tasks 2 to 5 — plus `view.test.js`'s scan, which already covers this file the
moment it exists. The page itself is exercised in a real browser by the
controller after this task, against mocked Cognito and API routes, because
the production API's CORS admits only the production origin. Keep `app.js`
thin enough that this is an honest division: logic goes in the tested modules,
and `app.js` only wires it up.

- [ ] **Step 1: Write `app.js`**

`site/assets/app.js`:

```js
// The admin page's DOM layer. It builds every element with createElement and
// text nodes and never turns a string into markup -- the CSP's
// require-trusted-types-for enforces that where browsers support it, and
// view.test.js enforces it everywhere. Anything worth testing belongs in
// auth.js, api.js, setupfile.js or view.js; this file only wires them up.
import { beginSignIn, completeSignIn, forgetSignIn, logoutUrl, mayReauth, wasSignedIn } from "./auth.js";
import { ApiError, createApi } from "./api.js";
import { SETUP_FILE_NAME, SetupFileError, setupFileFor } from "./setupfile.js";
import { emailVerified, gameChoices, messageFor, panelTitle } from "./view.js";

const $ = (id) => document.getElementById(id);

let cfg = null;
let api = null;
// The one place a token lives: this variable, for the life of the tab.
let session = null;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    // Refuse the markup properties outright rather than trust every caller.
    if (/html/i.test(key)) throw new Error(`refusing to set ${key}`);
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children); // strings become text nodes, never markup
  return node;
}

const kindOf = (err) => (err instanceof ApiError ? err.kind : "failed");

function setStatus(text, { error = false } = {}) {
  const status = $("status");
  status.textContent = text;
  status.classList.toggle("error", error);
}

function signIn() {
  beginSignIn(cfg, { origin: location.origin, storage: sessionStorage, navigate: (url) => location.assign(url) });
}

// Called when the API says the token is no longer good. Guarded, so an API
// that rejects even a fresh token cannot send the browser round in a loop.
function reauth() {
  session = null;
  if (!mayReauth(sessionStorage)) {
    forgetSignIn(sessionStorage);
    showSignedOut("Your session could not be renewed. Sign in again.");
    return;
  }
  setStatus(messageFor("any", "unauthorized"));
  signIn();
}

function signOut() {
  session = null;
  forgetSignIn(sessionStorage);
  location.assign(logoutUrl(cfg, location.origin));
}

function showSignedOut(message = "") {
  $("signed-out").hidden = false;
  $("signed-in").hidden = true;
  $("who").hidden = true;
  $("sign-out").hidden = true;
  setStatus(message, { error: Boolean(message) });
}

async function showSignedIn() {
  const claims = session.claims;
  $("who").textContent = typeof claims.email === "string" ? claims.email : "";
  $("who").hidden = false;
  $("sign-out").hidden = false;
  $("signed-out").hidden = true;
  $("signed-in").hidden = false;
  renderAdd(claims);
  await refresh();
}

// Returns true when the panel list loaded, so a caller knows whether its own
// success message still describes what is on screen.
async function refresh() {
  setStatus("Loading your panels…");
  const [devices, games] = await Promise.all([
    api.listDevices().catch((err) => ({ err })),
    api.listGames().then((doc) => (Array.isArray(doc?.games) ? doc.games : [])).catch((err) => ({ err })),
  ]);
  if (devices.err) {
    setStatus(messageFor("list", kindOf(devices.err)), { error: true });
    return false;
  }
  const gamesFailed = Boolean(games.err);
  renderPanels(devices, gamesFailed ? [] : games, gamesFailed);
  if (gamesFailed) setStatus(messageFor("games", kindOf(games.err)), { error: true });
  else setStatus(devices.length ? "" : "No panels on your account yet.");
  return true;
}

async function act(action, run, success) {
  setStatus("Working…");
  try {
    await run();
  } catch (err) {
    setStatus(messageFor(action, kindOf(err)), { error: true });
    return;
  }
  if (await refresh()) setStatus(success);
}

function renderPanels(devices, games, gamesFailed) {
  const list = $("panels");
  list.replaceChildren(...devices.map((device) => panelItem(device, games, gamesFailed)));
  list.hidden = devices.length === 0;
}

function panelItem(device, games, gamesFailed) {
  const title = panelTitle(device);
  const selectId = `game-${device.thingName}`;

  const select = el("select", { id: selectId, disabled: gamesFailed },
    ...gameChoices(device, games).map((choice) =>
      el("option", { value: choice.value, selected: choice.selected, disabled: choice.disabled }, choice.label)));
  select.addEventListener("change", () =>
    act("setGame", () => api.setGame(device.thingName, Number(select.value)),
      "Game set. The panel switches within a few seconds."));

  const nameInput = el("input", { type: "text", value: device.name ?? "", maxLength: 40, "aria-label": `Name for ${title}` });
  const renameForm = el("form", {
    class: "row",
    onsubmit: (event) => {
      event.preventDefault();
      act("rename", () => api.rename(device.thingName, nameInput.value), "Renamed.");
    },
  }, nameInput, el("button", { class: "btn", type: "submit" }, "Rename"));

  const remove = el("button", {
    class: "btn quiet",
    type: "button",
    onclick: () => {
      // Honest about what removal does not do: the panel keeps its certificate
      // until it is factory reset (SCO-24 is where revocation on unbind lives).
      if (!confirm(`Remove ${title} from your account? It keeps showing its current game until it is factory reset.`)) return;
      act("unbind", () => api.unbind(device.thingName), "Removed.");
    },
  }, "Remove");

  return el("li", { class: "panel" },
    el("h2", {}, title),
    el("span", { class: "thing" }, device.thingName),
    el("div", { class: "row" }, el("label", { for: selectId }, "Game"), select),
    renameForm,
    el("div", { class: "row" }, remove));
}

function renderAdd(claims) {
  const note = $("add-note");
  const button = $("download");
  const refuse = (text) => {
    note.textContent = text;
    note.hidden = false;
    button.disabled = true;
  };
  // The claim is refused for an unverified address, so a setup file for one
  // would make a panel nobody could claim.
  if (!emailVerified(claims)) return refuse("Verify your email address before setting up a panel.");
  let text;
  try {
    text = setupFileFor(claims.email);
  } catch (err) {
    if (!(err instanceof SetupFileError)) throw err;
    return refuse("Your email address cannot be written into a setup file.");
  }
  note.hidden = true;
  button.disabled = false;
  button.addEventListener("click", () => download(text));
}

function download(text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = el("a", { href: url, download: SETUP_FILE_NAME, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function wireClaim() {
  $("claim").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("claim-code");
    const code = input.value.trim();
    if (!code) {
      setStatus(messageFor("claim", "bad-request"), { error: true });
      return;
    }
    setStatus("Claiming…");
    try {
      await api.claim(code);
    } catch (err) {
      setStatus(messageFor("claim", kindOf(err)), { error: true });
      return;
    }
    input.value = "";
    if (await refresh()) setStatus("Panel added. It restarts into the scoreboard in about thirty seconds.");
  });
}

async function start() {
  $("sign-in").addEventListener("click", () => signIn());
  $("sign-out").addEventListener("click", () => signOut());
  wireClaim();

  try {
    const resp = await fetch("/config.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`config ${resp.status}`);
    cfg = await resp.json();
  } catch {
    setStatus("This site is not configured yet.", { error: true });
    return;
  }
  api = createApi({
    base: cfg.apiBase,
    getToken: () => (session && !session.expired() ? session.idToken : null),
    onUnauthorized: reauth,
  });

  const here = new URL(location.href);
  if (here.searchParams.has("code") || here.searchParams.has("error")) {
    const url = location.href;
    // The code is single-use and about to be spent, but it should not sit in
    // the address bar, the history, or a referrer either way.
    history.replaceState(null, "", "/");
    if (here.searchParams.has("error")) {
      forgetSignIn(sessionStorage);
      showSignedOut("Sign-in was cancelled.");
      return;
    }
    try {
      session = await completeSignIn(cfg, { url, storage: sessionStorage });
    } catch {
      forgetSignIn(sessionStorage);
      showSignedOut("Sign-in could not be completed. Try again.");
      return;
    }
    await showSignedIn();
    return;
  }

  // A refresh drops the in-memory token by design. A page that was signed in
  // goes back through Cognito, which returns straight away while its own
  // session cookie is valid. Not guarded by mayReauth: a successful sign-in
  // lands on ?code, not here, so this path cannot loop.
  if (wasSignedIn(sessionStorage)) {
    signIn();
    return;
  }
  showSignedOut();
}

start();
```

- [ ] **Step 2: Check it parses, and that the scan now covers it**

Run: `node --check site/assets/app.js` — Expected: no output.
Run: `make test-js` — Expected: PASS. The sink scan in `view.test.js` now reads
`app.js` too; if it fails, the cause is in `app.js`, not the test.

- [ ] **Step 3: Serve it and confirm every file the page asks for exists**

```bash
cd site && python3 -m http.server 8765 >/tmp/site-serve.log 2>&1 &
SERVER=$!
sleep 1
for path in / /assets/site.css /assets/admin.css /assets/app.js /assets/auth.js /assets/api.js \
            /assets/setupfile.js /assets/view.js /assets/ice.webp \
            /assets/fonts/plex-sans.woff2 /assets/fonts/barlow-condensed-700.woff2; do
  printf '%-45s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8765$path")"
done
kill $SERVER
```

Expected: every line `200`. (`/config.json` is absent until Task 7, and the
page says "This site is not configured yet." without it — that is correct.)

- [ ] **Step 4: Commit**

```bash
git add site/assets/app.js
git commit -m "site: wire sign-in, panels, claiming and the setup file to the page"
```

---

### Task 7: Ship it

**Files:**
- Modify: `Makefile`, `.gitignore`, `docs/hardware-checks.md`
- Modify (other repository): `/home/jay/projects/hockeytrack/site/assets/site.css`

**Interfaces:**
- Produces: `make site-config`, `make site`, `make site-local`.

- [ ] **Step 1: Ignore the generated config**

Append to `.gitignore`:

```
# Written by `make site-config` from terraform outputs. Nothing in it is a
# secret -- the API URL, the Cognito domain and a public client ID -- but a
# generated file does not belong in version control.
site/config.json
```

- [ ] **Step 2: Add the targets**

Add `site-config site site-local` to the Makefile's `.PHONY` line, then:

```make
# The admin site's runtime settings, taken from the stack itself so the page
# can never point at an API or a Cognito domain the stack does not have.
site-config:
	cd terraform && printf '{\n  "apiBase": "%s",\n  "cognitoDomain": "%s",\n  "clientId": "%s"\n}\n' \
	  "$$(terraform output -raw api_endpoint)" \
	  "$$(terraform output -raw cognito_domain)" \
	  "$$(terraform output -raw user_pool_client_id)" > ../site/config.json

# Upload the admin site. Tests first, so a broken page cannot ship.
#
# Cache lifetimes differ from HockeyTrack's on purpose. Its assets are cached
# for a day; these are five minutes, because this site's JavaScript carries the
# sign-in flow and has no hashed filenames, so a fix to it has to reach browsers
# promptly. config.json is never cached. Fonts never change and are immutable.
# tests/, package.json and config.json are excluded from the main sync --
# excluded files are also exempt from --delete, so the separate config.json
# upload is not removed by it.
site: test-js site-config
	aws s3 sync site/ s3://$$(cd terraform && terraform output -raw site_bucket)/ --exclude 'assets/*' --exclude 'tests/*' --exclude 'package.json' --exclude 'config.json' --delete --cache-control 'public, max-age=300' --region $(REGION)
	aws s3 cp site/config.json s3://$$(cd terraform && terraform output -raw site_bucket)/config.json --cache-control 'no-store' --content-type 'application/json' --region $(REGION)
	aws s3 sync site/assets/ s3://$$(cd terraform && terraform output -raw site_bucket)/assets/ --exclude 'fonts/*' --delete --cache-control 'public, max-age=300' --region $(REGION)
	aws s3 sync site/assets/fonts/ s3://$$(cd terraform && terraform output -raw site_bucket)/assets/fonts/ --delete --cache-control 'public, max-age=31536000, immutable' --content-type 'font/woff2' --region $(REGION)
	aws cloudfront create-invalidation --distribution-id $$(cd terraform && terraform output -raw site_distribution_id) --paths '/*' --query 'Invalidation.Id' --output text

# Serve the site locally on the one non-production callback URL Cognito
# accepts. Sign-in works here; API calls do not, because the API's CORS admits
# only the production origin, and widening production CORS for a development
# convenience is the wrong trade.
site-local: site-config
	cd site && python3 -m http.server 8000
```

Do **not** run `make site` or `make site-config` — both call Terraform against
live state. Check the Makefile parses instead:

Run: `make -n site site-local >/dev/null && echo parses` — Expected: `parses`.

- [ ] **Step 3: Mark the vendored copy, in both repositories**

In **this** repository, add as the first lines of `site/assets/site.css`:

```css
/* COPY of hockeytrack/site/assets/site.css, vendored unchanged on 2026-09-13
   along with its fonts and ice.webp. This site is a separate origin and
   HockeyTrack serves no CORS headers, so hotlinking was not an option. A
   redesign is a deliberate change in both repositories. */
```

Note that this *is* a change to the vendored file — a provenance header, and
nothing else. Confirm the rest is identical:

```bash
tail -n +5 site/assets/site.css | cmp - /home/jay/projects/hockeytrack/site/assets/site.css && echo "body identical"
```

In **HockeyTrack's** repository, add as the first lines of its
`site/assets/site.css`:

```css
/* Copied, with its fonts and ice.webp, into hockeytrack-scoreboard's admin
   site (site/assets/site.css) on 2026-09-13. A redesign here is a deliberate
   change in both repositories, not a silent drift. */
```

Commit that in the HockeyTrack repository on its own, locally only:

```bash
git -C /home/jay/projects/hockeytrack add site/assets/site.css
git -C /home/jay/projects/hockeytrack commit -m "site: note that the scoreboard admin site carries a copy of this stylesheet"
```

Then confirm the two bodies still match with both headers in place — this
repository's header is four lines, HockeyTrack's is three:

```bash
cmp <(tail -n +5 site/assets/site.css) \
    <(tail -n +4 /home/jay/projects/hockeytrack/site/assets/site.css) && echo "bodies identical"
```

- [ ] **Step 4: Update H8 now that step 4 has a page**

In `docs/hardware-checks.md`, H8's steps 4 and 5 already say to use the admin
site, but were written before it existed, so they cannot say what it shows.
Replace them with versions that name the page's actual controls and its exact
refusal message, so whoever runs H8 can tell a pass from a near miss:

```markdown
4. Sign in at https://scoreboard.davidjdrake.com **as a different invited
   user** and type the code into *Claim a panel*. Expect "No panel is waiting
   for you with that code…" — the same message a mistyped code gets, so the
   page reveals nothing about whether the code was real.
5. Sign out, sign in as the owner, and claim it. The panel should appear in
   the list, and within about thirty seconds the panel itself should restart
   into the scoreboard. Choose a game for it and confirm the panel switches.
```

And add before step 1:

```markdown
0. Signed in as the owner, use *Download setup file* rather than writing
   `scoreboard-setup.txt` by hand. This also checks the file the site writes
   is one the panel reads.
```

Keep the rest of H8, including "The one to watch", unchanged.

- [ ] **Step 5: Run everything**

Run: `make test-js && make test-py` — Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add Makefile .gitignore docs/hardware-checks.md site/assets/site.css
git commit -m "site: deploy it, and mark what was copied from HockeyTrack"
```

---

## Deploying, for whoever does it — not an implementer

Order matters, because the page's claim and the CSP both depend on the stack:

1. `terraform apply` — this lands plan 1's enrollment stack if it has not
   landed yet, Task 1's CSP, the `cognito_domain` output, and the `removed`
   block that hands `index.html` over without deleting the holding page.
2. Confirm the live CSP names the hosted-UI domain, and wait until it does
   before going on to `make site`:
   `curl -sI https://scoreboard.davidjdrake.com/ | grep -i content-security-policy`.
   The response-headers policy update from step 1 propagates to CloudFront's
   edges after `apply` returns, not the instant it does, so this can need a
   few retries. Deploying the real page under the old `connect-src` would
   block the token exchange for whoever hits a stale edge.
3. `make site` — tests, config, upload, invalidation.
4. Sign in once, end to end, before anyone else does.

**Tickets this plan generates rather than fixes:** Cognito's hosted UI is
unstyled; the managed login branding is a separate piece of work. And the
admin site has no alarm of its own — a broken deploy shows up as a user
reporting it.
