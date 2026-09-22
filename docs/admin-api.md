# Admin API

The API behind the [admin site](superpowers/specs/2026-09-07-admin-site-design.md)
(not yet built — see the note at the end of this document). It answers two
questions: who owns which panel, and what game that panel is following. The
source of truth for everything below is `cloud/cmd/api/handler.go` and
`cloud/internal/devices/store.go`; if this document and the code ever
disagree, the code is right.

## Authentication and authorization are two different checks

Every route requires a valid Cognito JWT, and it is checked twice. API
Gateway's JWT authorizer (`terraform/admin.tf`) runs in front of all six
routes, `GET /api/games` included, and rejects a request with no token, or a
token that fails its own check, before the handler runs. But the handler does
not trust the authorizer's word for it, or read the claims the authorizer
hands it in the event: `cloud/internal/idtoken` verifies the raw
`Authorization` header itself — signature against the pool's published keys,
issuer, exact audience, `token_use`, expiry — and takes identity only from
that. A token that reaches the handler without passing this second check
never runs as anyone; see "Errors common to every route" below for what it
returns.

Authorization — does *this* signed-in user own *this* device — is a second,
separate check the Lambda makes itself, by reading an ownership row out of
DynamoDB. `GET /api/games` is exempt from this second check, because the
schedule isn't owned by anyone; it still requires the first one.

Sign-up is closed. The only way in is Sign in with Google, and only for an
address on the invite list: the SSM parameter `/scoreboard/allowed-emails`,
which the `scoreboard-authgate` function checks when Cognito is about to
create an account and again every time it issues tokens
(`terraform/signin.tf`; design in
`docs/superpowers/specs/2026-09-13-google-sign-in-design.md`). An uninvited
Google account gets no account and no token, so there is nothing for the API
to authorize. Inviting someone is one `aws ssm put-parameter` call, given in
section 4.2 of that spec.

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
  [{"thingName": "scoreboard-01", "name": "Living room", "gameId": 2026020001,
    "chosenAt": 1789871240471,
    "game": {"state": "LIVE", "start": "2026-10-01T23:00:00Z",
             "away": {"abbrev": "MTL", "score": 2}, "home": {"abbrev": "TOR", "score": 3},
             "period": {"label": "2"}, "lastSeenAt": 1789930443000}}]
  ```
  An owner with no devices gets `[]`, not an error.

  `chosenAt` is when the owner last chose the game, in milliseconds on the
  server's clock: the same stamp that was sent to the panel. `game` is the
  little of the reducer's state the site needs to say what the panel should be
  showing. Both are there so the site can run the panel's own rule
  (`site/assets/showing.js`, held to `main.presentation` by
  `testdata/presentation-vectors.json`); neither is word from the panel, which
  cannot publish anything.

  `game` is **absent** when the panel follows nothing, when the reducer has
  not seen the game yet (it has not started; the site falls back on
  `/api/games`), or when the games table could not be read. That last case is
  logged and the list is still returned: a games table that is down does not
  take the panels off the page. The API's role may `GetItem` on that table and
  nothing else.
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
- **500** `{"error": "lookup failed"}` — the ownership check itself failed
  (`Store.Get` returned an error), before the API could determine whether the
  caller owns `{thing}`. Distinct from the 404 below: this means the check
  didn't complete, not that it completed and said "not yours."
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

### Display settings

Three layers, resolved on the server, field by field: the built-in values
(countdown 12 h before puck drop, a final score up for 3 h, no sleep hours),
the account's defaults, and one panel's overrides. The panel is sent only the
result, and checks it again (`parse_display`).

A **layer** is the same shape everywhere. Every key is optional, and an absent
key says nothing, so the layer beneath shows through:

```json
{"countdownLeadMin": 120,
 "finalHoldMin": 30,
 "sleep": {"enabled": true, "start": "23:00", "end": "07:00", "zone": "America/Toronto"}}
```

- `countdownLeadMin` 0 to 2880, `finalHoldMin` 0 to 1440: whole minutes.
- `sleep.enabled: false` is a value, not an absence: it is how a panel says
  "no sleep hours" over an account default that has some.
- `start`/`end` are `HH:MM`; `zone` is an IANA name, checked against an
  alphabet and then against the time zone data built into the binary. Equal
  ends are stored as switched off.
- Decoding is **strict**: an unknown key is a 400. A client cannot put
  `gameId`, `chosenAt` or a `display` block through these routes.
- Bodies over 4 KB are a 400 before anything is parsed.

#### `GET /api/settings`

- **200** `{"defaults": <layer>, "builtIn": {"v":1,"countdownLeadMin":720,"finalHoldMin":180}}`

#### `PUT /api/settings`

Body: a layer. Saves the caller's defaults, **then** publishes the whole config
document to each of the caller's panels.

- **200** `{"defaults": <layer>, "notSent": ["scoreboard-…"]}`. `notSent`
  names panels whose publish failed. The defaults are saved regardless, and
  because every publish is the whole document composed from what is stored,
  saving again converges.
- **400** `{"error": "invalid settings"}`.

#### `PUT /api/devices/{thing}/display`

Body: a layer, which **replaces** the panel's overrides (send `{}` to clear
them). Publishes, then saves, as the game route does.

- **200** the device, as `GET /api/devices` returns it.
- **400** invalid settings. **404** not the caller's panel, unclaimed, or no
  such panel: the same answer for all three.
- **500** `lookup failed` if the account's defaults could not be read.
  Nothing is published that was built on settings nobody could read.
- **502** the publish failed; nothing was saved.

`GET /api/devices` adds to each panel:

```json
"display": {"overrides": <layer>,
            "resolved": {"v":1,"countdownLeadMin":120,"finalHoldMin":180},
            "sources": {"countdownLeadMin":"account","finalHoldMin":"built-in","sleep":"built-in"}}
```

absent if the account's defaults could not be read.

#### The config document

Every publish to a panel is written by `cloud/internal/panelconfig` and is the
whole document, because a retained message replaces what was there:

```json
{"gameId": 2026020001, "chosenAt": 1789871240471,
 "display": {"v":1,"countdownLeadMin":120,"finalHoldMin":180,
             "sleep":{"start":"23:00","end":"07:00","zone":"America/Toronto"}}}
```

`chosenAt` changes only when the owner chooses a game; every other publish
re-sends the stored one, because a panel reads a stamp it has not seen as a
button press. A panel following nothing is sent `"gameId": null`, because a
panel reads `0` as a game to select. `testdata/config-documents.json` is
composed byte for byte by the Go suite and read by the panel's own parsers in
the device suite.

### `PUT /api/devices/{thing}/wake`

The owner's hand on the sleep switch. Body: `{"mode":"awake"|"asleep"|"auto"}`
and nothing else (unknown keys are refused).

- `awake`: lit through its sleep hours. Everything else still applies: a game
  thirteen hours off is still not counted down.
- `asleep`: dark now, whatever the hour and **whatever is on, a live game
  included**. The screens that ask the owner for something are not hidden.
- `auto`: follow sleep hours. Clears the switch.

**When it ends is the server's to decide, never the client's:** the next time
the panel's sleep hours END (so `awake` at 1 a.m. lasts the night and `asleep`
at 8 p.m. lasts until morning), or twelve hours on a panel with none, and
never more than a day. The panel and the site hold the same day's bound and
ignore a switch that claims more, so no number from anywhere can pin a panel
lit or dark. A panel with no clock set ignores the switch.

Publishes the whole config document (`display.wake`) with `chosenAt`
**unchanged**, so the panel does not read it as a choice of game. `404` for a
panel that is not the caller's. A switch that has ended is neither sent nor
returned. Releasing a panel clears it.

Why it exists: choosing a game used to light a sleeping panel for five
minutes. It no longer does (owner's ruling, 2026-09-21); this is how to say
"on" explicitly. A live game still beats sleep hours. Needs image v0.1.7.

### Game schedules

What an owner has asked a panel to show. **Nothing here reaches a panel
through the API:** these routes record and check the owner's wishes, and
saving a schedule publishes nothing from this process (a test holds that).
The director (SCO-42, `cloud/cmd/director`, its own function and role) turns
the schedule into the one `gameId` a panel follows, once a minute and, since
a save asks it to run for that panel, at once. `PUT /api/devices/{thing}/game`
still works and wins: a game the owner puts on a panel by hand stays until it
is over, and then the schedule resumes.

#### `GET /api/schedule`

The whole NHL season, for the picker:
`{"teams":{"TOR":"Toronto Maple Leafs"},"games":[{"gameId","date","start","away","home","venue","type"}]}`.
`date` is the NHL's game date, which days are grouped by; it is not derived
from `start` (a late Pacific game is the next day in UTC). A club name is one
printable line of at most 40 characters or the club is left out.
It is public data. It is served here because the browser cannot read it from
HockeyTrack's origin without a CORS header there and a wider `connect-src`
here; the API already fetches the file server-side. The address is this
function's configuration and no request can influence it.

Every row is rebuilt from checked fields (`internal/season`): an id above
zero, a start that parses (re-written in UTC), two different abbreviations of
two to four capitals, a known game type, a venue cut to one printable line.
A row that fails is left out and counted in the log; a file with more than
3,000 rows is refused whole. The result is cached for ten minutes, and a bad
fetch does not replace a good season for six hours. `502` when there is no
season to give.

#### `PUT /api/devices/{thing}/schedule`

Body: `{"games":[ids],"templates":[],"resolutions":[{"sequence":[ids],"keep":[ids]}]}`.

Checked in this order: size (64 KB), shape (strict keys; at most 1,500
distinct positive ids), ownership (`404` if the panel is not the caller's),
then the rules.

| Status | When |
|---|---|
| `200` | Stored. The body is the schedule with `next` (the coming kept games) and `undecided`. |
| `400` | Malformed; a game id that is not in the season and was not already on the panel; or a resolution that keeps nothing, keeps a game from outside its conflict, or keeps two games that overlap. |
| `404` | Not the caller's panel, or a template id (the caller has none yet; not-yours and not-there are one answer). |
| `409` | **A conflict among these games was left unanswered.** Body: `{"error","unresolved":[[ids]]}`. Nothing is stored. The owner is present, so nothing is decided for them; the default rule exists only for conflicts nobody caused. |
| `502` | The season could not be read. Nothing is stored on a guess. |

A game already on the panel that has since left the season is over, and is
dropped without complaint. A resolution for a conflict that no longer exists
is dropped. Resolutions are stored against their exact sequence and are never
stretched to fit a different one. Releasing a panel clears its schedule with
everything else: the next owner does not learn what the last one watched.

`GET /api/devices` carries each panel's `schedule`: `games`, `templates`,
`resolutions`, and, when the season could be read (`known: true`), `next` and
`undecided`. With the season down the panels are still listed.

### `PATCH /api/devices/{thing}`

Renames a device. Purely cosmetic — the display name shown by the site, not
anything the device itself knows about.

- **Body:** `{"name": "Living room"}`
- **200** — the updated device, same shape as above.
- **400** `{"error": "a name is required"}` — missing, empty, or unparseable
  body.
- **500** `{"error": "lookup failed"}` — the ownership check itself failed
  (`Store.Get` returned an error), same as on `PUT .../game` above — distinct
  from the 404 below.
- **404** `{"error": "no such device"}` — caller doesn't own `{thing}`.
- **500** `{"error": "save failed"}` — store write failed.

### `DELETE /api/devices/{thing}`

Unbinds a device, returning it to unclaimed. Its pairing code becomes usable
again; nothing is published to the device (it still publishes nothing and
subscribes to nothing new — it just goes on following whatever game it was
last told to, until someone claims it again and changes that).

- **200** — `{"thingName": "scoreboard-01"}`
- **500** `{"error": "lookup failed"}` — the ownership check itself failed
  (`Store.Get` returned an error), same as on `PUT .../game` above — distinct
  from the 404 below.
- **404** `{"error": "no such device"}` — caller doesn't own `{thing}`.
- **500** `{"error": "unbind failed"}` — store write failed.

### `GET /api/games`

Today's scheduled games, from the public HockeyTrack schedule
(`internal/today.Build`), not fetched by the browser directly (that schedule
serves no CORS headers). This is the one route with no ownership check —
anyone with a valid session can call it, because the schedule isn't owned by
anyone — but it still requires the same JWT as every other route.

This is *not* built the same way the device's own `hockeytrack/games/today`
document is, despite calling the same `today.Build`. `cmd/api/main.go` passes
it an empty states map, where `cmd/today/main.go` passes the real per-game
states from `gamestore.ListActive`. `today.Build`'s carry-over rule only keeps
a game from yesterday if it's `tracked` in that map, and an empty map never
satisfies that — so a game that started yesterday and is still in progress
after midnight ET shows up on the panel's own list but not in this endpoint's.
This is a known gap in what the endpoint lists, not a bug in how it's built;
fixing it is a separate ticket.

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

- **401** `{"error": "unauthenticated"}` — the token failed the function's
  own verification (`cloud/internal/idtoken`): a missing or malformed
  header, a bad signature, the wrong issuer or audience, a `token_use` other
  than `id`, or an expired token. This can happen even after API Gateway's
  authorizer accepted the request — an access token has no `aud`, so the
  authorizer checks `client_id` instead and lets it through, and the
  function then refuses it itself — in which case the function also logs a
  mismatch line (`docs/superpowers/specs/2026-09-15-direct-invoke-design.md`
  §3.3). A missing token, or one the authorizer's own check fails, is
  rejected by API Gateway itself, before the Lambda runs.
- **503** `{"error": "sign-in check unavailable"}` — the pool's signing keys
  could not be fetched, so no token could be checked. This fails closed
  without blaming the caller; retrying shortly is reasonable.
- **400** `{"error": "invalid request body"}` — API Gateway marked the body
  base64-encoded and it didn't decode. Rare, and generally not something a
  hand-written client will trigger.

One more response is in the code but not reachable through this deployment:
if the route key doesn't match any of the six above, the handler falls
through to `404 {"error": "no such route"}`. `terraform/admin.tf` wires API
Gateway to forward exactly these six route keys and nothing else, so this is
a safety net in the handler's own switch, not something a real request can
trigger.

## Getting a token and calling the API

The client is a public Cognito app client (no secret — anything shipped to a
browser can't keep one), using Authorization Code with PKCE against the
hosted UI, with Google as its only identity provider. There is no password to
send anywhere, so there is no token endpoint you can hit with a single `curl`;
a human signs in with an invited Google account once per session. The
outline, with the pool's domain, client id and this deployment's API
endpoint left as environment variables you fill in from your own Terraform
outputs:

```bash
# 1. Generate a PKCE pair.
CODE_VERIFIER=$(openssl rand -base64 96 | tr -d '=+/\n' | cut -c1-64)
CODE_CHALLENGE=$(printf '%s' "$CODE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 | tr -d '=' | tr '/+' '_-')

# 2. Open this URL in a browser and sign in with Google. It redirects to
#    http://localhost:8000/?code=... on success.
echo "https://${USER_POOL_DOMAIN}/oauth2/authorize?identity_provider=Google&client_id=${CLIENT_ID}&response_type=code&scope=openid+email&redirect_uri=http://localhost:8000/&code_challenge_method=S256&code_challenge=${CODE_CHALLENGE}"

# 3. Exchange the code from that redirect for tokens.
read -p "code from the redirect: " AUTH_CODE
TOKEN=$(curl -s "https://${USER_POOL_DOMAIN}/oauth2/token" \
  -H "content-type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&client_id=${CLIENT_ID}&code=${AUTH_CODE}&redirect_uri=http://localhost:8000/&code_verifier=${CODE_VERIFIER}" \
  | jq -r '.id_token')

# 4. Call the API. Use id_token, not access_token: an access token carries
#    no aud, so API Gateway's JWT authorizer checks its client_id instead and
#    lets it through (no route here sets scopes), and the function then
#    refuses it itself with 401 and logs a mismatch line, rather than never
#    reaching the function at all.
curl -s -H "Authorization: Bearer ${TOKEN}" "$API/api/devices"
```

`$API` (Terraform output `api_endpoint`), `$CLIENT_ID` (output
`user_pool_client_id`) and `$USER_POOL_DOMAIN` (output `cognito_domain`,
the full hosted-UI host — `auth.<site domain>`, not a bare prefix) come
straight out of `terraform output` for this stack. None of these are
recorded here, since they're deployment-specific.

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

This API also has no alarms of its own. Nothing pages on a spike of 401s
(someone hammering `POST /api/devices/claim` with guessed codes, say) or of
5XXs (the store or the IoT publish failing under normal use). That's a real
gap, not a deliberate one — the IoT surface of this same project has six
alarms (see `terraform/iot-alarms.tf`), and this API has none. It's a
reasonable next addition once this route sees real traffic; until then, the
access log (`aws_cloudwatch_log_group.api_access`) is the only record of what
this API has been asked to do.
