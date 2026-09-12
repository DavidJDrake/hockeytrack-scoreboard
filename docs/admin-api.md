# Admin API

The API behind the [admin site](superpowers/specs/2026-09-07-admin-site-design.md)
(not yet built — see the note at the end of this document). It answers two
questions: who owns which panel, and what game that panel is following. The
source of truth for everything below is `cloud/cmd/api/handler.go` and
`cloud/internal/devices/store.go`; if this document and the code ever
disagree, the code is right.

## Authentication and authorization are two different checks

Every route requires a valid Cognito JWT. API Gateway's JWT authorizer
(`terraform/admin.tf`) checks that the token is a real, unexpired token issued
by this project's user pool, for this project's client — nothing more. It
runs in front of all six routes, `GET /api/games` included. A request with no
token, or an invalid one, never reaches the Lambda; API Gateway returns its
own 401 before the handler runs. The one 401 the handler itself can return is
for a token that reached it without a usable `sub` claim.

Authorization — does *this* signed-in user own *this* device — is a second,
separate check the Lambda makes itself, by reading an ownership row out of
DynamoDB. `GET /api/games` is exempt from this second check, because the
schedule isn't owned by anyone; it still requires the first one.

Sign-up is closed. There is no self-registration endpoint or hosted sign-up
flow — users are created by the administrator in the Cognito console
(`aws_cognito_user_pool.admin`, `admin_create_user_config.allow_admin_create_user_only
= true`). If you don't already have an account, the API can't give you one.

### A non-owner gets 404, never 403

Every device-scoped route resolves the device by thing name, then checks
`device.Owner == caller`. If the device doesn't exist, or exists but belongs
to someone else, the caller gets the same response: `404 {"error": "no such
device"}`. A 403 would leak one bit of information a stranger has no business
learning — that a given thing name exists at all. 404 makes "not yours" and
"doesn't exist" indistinguishable from outside.

The claim endpoint applies the same principle to codes: claiming an unknown
code and claiming a code that's already been used both return the identical
`404 {"error": "no unclaimed device with that code"}`. The store enforces
this structurally, not just by coincidence of handler logic — `ByCode`
(`cloud/internal/devices/store.go`) only ever returns a device whose `Owner`
is still empty, so a claimed device's code simply stops resolving. There's no
way to ask "was this code ever valid" from the response.

## Routes

All bodies and responses are JSON. All timestamps are absent — there are
none; the API has no notion of last-seen or history (see the design doc's
"out of scope for v1").

### `GET /api/devices`

The caller's own devices.

- **200** — a JSON array, one entry per device the caller owns:
  ```json
  [{"thingName": "scoreboard-01", "name": "Living room", "gameId": 2026020001}]
  ```
  An owner with no devices gets `[]`, not an error.
- **500** `{"error": "list failed"}` — the store query itself failed.

### `POST /api/devices/claim`

Binds an unclaimed device to the caller.

- **Body:** `{"code": "01"}`
- **200** — `{"thingName": "scoreboard-01", "name": "", "gameId": 0}`. Only
  `thingName` is meaningful here; `name` and `gameId` are the zero values of a
  freshly claimed device, not omitted from the response.
- **400** `{"error": "a code is required"}` — missing or empty `code`, or a
  body that doesn't parse as JSON.
- **404** `{"error": "no unclaimed device with that code"}` — no device has
  this code, or a device has it but is already claimed. See above: these two
  cases are deliberately the same response.
- **500** `{"error": "claim failed"}` — a store error on lookup or on the
  claim write itself, other than "already claimed".

### `PUT /api/devices/{thing}/game`

Sets the game a panel follows. This is the one route with a known failure
window — read it before assuming a 500 here means nothing happened.

- **Body:** `{"gameId": 2026020001}`
- **200** — `{"thingName": ..., "name": ..., "gameId": ...}`, the updated
  device.
- **400** `{"error": "a gameId is required"}` — missing, zero, or a body that
  doesn't parse.
- **404** `{"error": "no such device"}` — caller doesn't own `{thing}` (or it
  doesn't exist).
- **502** `{"error": "publish failed"}` — the retained MQTT publish to
  `scoreboard/{thing}/config` failed. Nothing changed anywhere.
- **500** `{"error": "save failed"}` — **the publish already succeeded** and
  only the DynamoDB write failed afterward.

  The handler publishes the retained config message before it writes the
  ownership record, on purpose. If the store write then fails, the panel has
  already changed to the new game — that message is retained, so the device
  picks it up now or on its next reconnect — but the stored `gameId` still
  shows the old value, and the caller sees a 500. This is a deliberate
  trade: the panel is what the owner is actually looking at, and the
  retained publish is idempotent, so a retry (or the next successful
  change) converges the record with reality either way. The reverse
  ordering would avoid a stale record but risk the opposite mismatch — a
  record claiming a change the panel never received. If you get a 500 from
  this route, retry it; don't assume nothing happened.

### `PATCH /api/devices/{thing}`

Renames a device. Purely cosmetic — the display name shown by the site, not
anything the device itself knows about.

- **Body:** `{"name": "Living room"}`
- **200** — the updated device, same shape as above.
- **400** `{"error": "a name is required"}` — missing, empty, or unparseable
  body.
- **404** `{"error": "no such device"}` — caller doesn't own `{thing}`.
- **500** `{"error": "save failed"}` — store write failed.

### `DELETE /api/devices/{thing}`

Unbinds a device, returning it to unclaimed. Its pairing code becomes usable
again; nothing is published to the device (it still publishes nothing and
subscribes to nothing new — it just goes on following whatever game it was
last told to, until someone claims it again and changes that).

- **200** — `{"thingName": "scoreboard-01"}`
- **404** `{"error": "no such device"}` — caller doesn't own `{thing}`.
- **500** `{"error": "unbind failed"}` — store write failed.

### `GET /api/games`

Today's games — built the same way the device's own `hockeytrack/games/today`
document is, from the public HockeyTrack schedule (`internal/today.Build`),
not fetched by the browser directly (that schedule serves no CORS headers).
This is the one route with no ownership check — anyone with a valid session
can call it, because the schedule isn't owned by anyone — but it still
requires the same JWT as every other route.

- **200**:
  ```json
  {"generatedAt": 1791135723123, "games": [
    {"gameId": 2026020001, "away": "TBL", "home": "NYR", "start": "2026-10-01T23:30:00Z", "state": "PRE"}
  ]}
  ```
  Each game's `state` is `"PRE"` here — this endpoint doesn't know which
  games are live; that's the reducer's job, not this API's.
- **502** `{"error": "schedule unavailable"}` — fetching or parsing the
  upstream schedule failed.
- **500** `{"error": "no game source"}` — the Lambda was deployed without a
  games source wired up. `cmd/api/main.go` always wires one and refuses to
  start without `SCHEDULE_URL` set, so this is a deployment/wiring bug, not
  something a caller can hit through normal use.

## Errors common to every route

- **401** `{"error": "unauthenticated"}` — no usable `sub` claim on the
  token. In practice this means the request got past API Gateway's authorizer
  with a token that doesn't carry `sub`, which shouldn't happen with a
  correctly configured Cognito authorizer; a missing or invalid token is
  rejected by API Gateway itself, before the Lambda runs.
- **400** `{"error": "invalid request body"}` — API Gateway marked the body
  base64-encoded and it didn't decode. Rare, and generally not something a
  hand-written client will trigger.

## Getting a token and calling the API

The client is a public Cognito app client (no secret — anything shipped to a
browser can't keep one), using Authorization Code with PKCE against the
hosted UI. There's no token endpoint you can hit with a single `curl` and a
password; a human has to sign in through the hosted UI once per session. The
outline, with the pool's domain, client id and this deployment's API
endpoint left as environment variables you fill in from your own Terraform
outputs:

```bash
# 1. Generate a PKCE pair.
CODE_VERIFIER=$(openssl rand -base64 96 | tr -d '=+/\n' | cut -c1-64)
CODE_CHALLENGE=$(printf '%s' "$CODE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 | tr -d '=' | tr '/+' '_-')

# 2. Open this URL in a browser and sign in. It redirects to
#    http://localhost:8000/?code=... on success.
echo "https://${USER_POOL_DOMAIN}.auth.${REGION}.amazoncognito.com/login?client_id=${CLIENT_ID}&response_type=code&scope=openid+email&redirect_uri=http://localhost:8000/&code_challenge_method=S256&code_challenge=${CODE_CHALLENGE}"

# 3. Exchange the code from that redirect for tokens.
read -p "code from the redirect: " AUTH_CODE
TOKEN=$(curl -s "https://${USER_POOL_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H "content-type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&client_id=${CLIENT_ID}&code=${AUTH_CODE}&redirect_uri=http://localhost:8000/&code_verifier=${CODE_VERIFIER}" \
  | jq -r '.id_token')

# 4. Call the API. The authorizer's audience is the client id, which is
#    only present on the ID token, not the access token — use id_token.
curl -s -H "Authorization: Bearer ${TOKEN}" "$API/api/devices"
```

`$API` (Terraform output `api_endpoint`) and `$CLIENT_ID` (output
`user_pool_client_id`) come straight out of `terraform output` for this
stack. `$USER_POOL_DOMAIN` isn't a Terraform output — it's not needed
outside this kind of manual flow — but it's derivable: `admin.tf` sets it to
`scoreboard-admin-<account id>` (`aws_cognito_user_pool_domain.admin`).
`$REGION` is whatever you set `var.region` to. None of these are recorded
here, since they're deployment-specific.

## What this document is not

There is no admin site yet. This document describes the API Tasks 1–4 built
— the store, its DynamoDB implementation, the Lambda handler, and the
Terraform wiring it behind API Gateway with a Cognito authorizer — not a
working end-to-end product. The static site, the panel's pairing screen, and
provisioning changes to hand out per-device pairing codes automatically are
a separate, not-yet-started plan (see "what this plan deliberately leaves
out" in
[`docs/superpowers/plans/2026-09-12-admin-api.md`](superpowers/plans/2026-09-12-admin-api.md)).
Until then, the
one existing panel (`scoreboard-01`, pairing code `01`) is registered in
DynamoDB by hand, and rate-limiting on claim attempts — called for in the
design doc — doesn't exist, because with one hand-registered device and
invite-only sign-up it isn't yet load-bearing.
