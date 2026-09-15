# Closing Direct Invocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop forged events sent straight to `scoreboard-api` and `scoreboard-enroll` from being believed, and page on any invocation of the three admin-path functions that did not come from API Gateway or Cognito.

**Architecture:**
- **Prevent (this repository):** both functions verify the raw Cognito ID token themselves, through a new `internal/idtoken` package built on `github.com/golang-jwt/jwt/v5`, and never read identity from the authorizer's claims. A log line and the `scoreboard-token-mismatch` alarm catch forged authorizer claims.
- **Detect (HockeyTrack):** the account trail logs Lambda data events for the three functions. Sections 10 and 11 ignore them by `eventCategory`. A new section 12 rule pages on any invocation whose `invokedBy` is not the expected service.

**Tech Stack:** Go 1.27 (aws-lambda-go, golang-jwt/jwt v5), Terraform (AWS provider), Node 22 `node:test` tripwires, CloudTrail, EventBridge, CloudWatch.

**Spec:** `docs/superpowers/specs/2026-09-15-direct-invoke-design.md` (this repository)

## Global Constraints

- **Both repositories are PUBLIC.**
  - Never commit `terraform/terraform.tfvars` (in either repo), `device/config/`, any certificate, private key, credential or real email address.
  - Test addresses use `example.com`.
- **Agents and subagents must never run:** `terraform apply`, any mutating AWS call (including `aws lambda invoke`), `make deploy`, `make site`, `make provision`, `tools/provision.sh`, or `git push`.
  - Applies are planned to a saved file in the session scratchpad, then run by the user with `!`.
  - Direct invokes are run by the user, or by the controller only when the user explicitly says to.
- **Every AWS CLI command passes `--region us-east-1`.**
- **Never use `terraform show -json` or `terraform console`** where secrets are in state. `terraform show -no-color <planfile>` is allowed.
- **US spelling** (enroll, enrollment) in prose, identifiers and resource names.
- **Go builds** go through `make build` (`-buildvcs=false -trimpath`). Put Go 1.27 on PATH with `export PATH=$HOME/.local/share/go/bin:$PATH`.
- **Scoreboard alarms** are named `scoreboard-*` with a literal `alarm_name`. Security alarms send to `data.aws_sns_topic.security_alerts.arn`.
- **HockeyTrack event patterns** are `<= 2048` characters, enforced by a precondition.
- **Branches:** scoreboard `direct-invoke` (exists; the spec is committed there). HockeyTrack `scoreboard-invoke-detection`, created from `main` in Task 4.
- **Token checks, verbatim from the spec:**
  - `alg` exactly `RS256`;
  - `kid` in the JWKS;
  - `iss` = `https://cognito-idp.<region>.amazonaws.com/<userPoolID>`;
  - `aud` exactly the site client ID;
  - `token_use` = `"id"`;
  - `exp` required, 30 s leeway;
  - JWKS refetch on an unknown `kid` at most once every 5 minutes;
  - fetch timeout 5 s;
  - JWKS body at most 64 KiB.
- **The mismatch log line is exactly** `token rejected after authorizer accepted`.
- **Responses:** an invalid token returns `401 {"error":"unauthenticated"}`. A key fetch failure returns `503 {"error":"sign-in check unavailable"}`.

## File map

**hockeytrack-scoreboard**

| File | Responsibility |
|---|---|
| `cloud/internal/idtoken/idtoken.go` (create) | `Verifier`, `Claims`, `Tokens`, `ErrInvalid`, `ErrUnavailable`, JWKS cache |
| `cloud/internal/idtoken/authenticate.go` (create) | `Authenticate`: header lookup, the mismatch log line |
| `cloud/internal/idtoken/idtoken_test.go`, `authenticate_test.go` (create) | Unit tests against a test RSA key and `httptest` JWKS |
| `cloud/internal/idtoken/idtokentest/fake.go` (create) | Test-only fake verifier; never imported by production code |
| `cloud/cmd/api/handler.go`, `main.go`, `handler_test.go` (modify) | Authenticate every route |
| `cloud/cmd/enroll/handler.go`, `main.go`, `handler_test.go` (modify) | Authenticate `POST /api/devices/claim`; owner hint from `Claims` |
| `cloud/go.mod`, `cloud/go.sum` (modify) | Add `github.com/golang-jwt/jwt/v5` |
| `terraform/admin.tf` (modify) | Env vars on `api`; the two metric filters and the alarm |
| `terraform/enroll.tf` (modify) | Env vars on `enroll` |
| `site/tests/invoke-config.test.js` (create) | Tripwires |
| `site/tests/signin-config.test.js` (modify) | Alarm-count floor 13 → 14 |
| `docs/superpowers/specs/2026-09-15-direct-invoke-design.md` (modify) | Three wording fixes (Task 1), verification record (Task 8) |

**hockeytrack**

| File | Responsibility |
|---|---|
| `terraform/cloudtrail.tf` (modify) | Function lookups; second `event_selector` |
| `terraform/security-alarms.tf` (modify) | `eventCategory` guards and comments on sections 10 and 11; section 12 rule; registry entries |
| `docs/threat-model.md` (modify) | §4 paragraphs; §7 recovery entry |

---

### Task 1: `internal/idtoken`

**Files:**
- Create: `cloud/internal/idtoken/idtoken.go`
- Create: `cloud/internal/idtoken/authenticate.go`
- Create: `cloud/internal/idtoken/idtoken_test.go`
- Create: `cloud/internal/idtoken/authenticate_test.go`
- Create: `cloud/internal/idtoken/idtokentest/fake.go`
- Modify: `cloud/go.mod`, `cloud/go.sum`
- Modify: `docs/superpowers/specs/2026-09-15-direct-invoke-design.md`

**Interfaces:**
- **Produces, for Tasks 2 and 3:**
  - `idtoken.Claims{Sub, Email string; EmailVerified bool; CognitoUsername string}`
  - `idtoken.Tokens` interface, with `Verify(ctx context.Context, authorizationHeader string) (Claims, error)`
  - `idtoken.New(region, userPoolID, clientID string) *Verifier`
  - `idtoken.Authenticate(ctx context.Context, t Tokens, req events.APIGatewayV2HTTPRequest) (Claims, error)`
  - `idtoken.ErrInvalid`, `idtoken.ErrUnavailable`
  - `const idtoken.MismatchMessage = "token rejected after authorizer accepted"`
  - `idtokentest.Fake{Unavailable bool}`, which implements `Tokens`
  - `idtokentest.Header(c idtoken.Claims) string`, which returns a header `Fake` accepts

- [ ] **Step 1: Add the dependency**

```bash
export PATH=$HOME/.local/share/go/bin:$PATH
cd cloud && go get github.com/golang-jwt/jwt/v5@latest
```
Expected: `go.mod` gains a `github.com/golang-jwt/jwt/v5 vX.Y.Z` require line. Note the version; it goes in the commit message.

- [ ] **Step 2: Write the failing verifier tests**

`cloud/internal/idtoken/idtoken_test.go`:

```go
package idtoken

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	testPool   = "us-east-1_TEST"
	testClient = "client-123"
	testIssuer = "https://cognito-idp.us-east-1.amazonaws.com/" + testPool
)

var (
	keysOnce         sync.Once
	signingKey, rotatedKey *rsa.PrivateKey
)

func keys(t *testing.T) (*rsa.PrivateKey, *rsa.PrivateKey) {
	t.Helper()
	keysOnce.Do(func() {
		var err error
		if signingKey, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
			panic(err)
		}
		if rotatedKey, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
			panic(err)
		}
	})
	return signingKey, rotatedKey
}

type jwk struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	Use string `json:"use,omitempty"`
	N   string `json:"n,omitempty"`
	E   string `json:"e,omitempty"`
}

func rsaJWK(kid string, pub *rsa.PublicKey) jwk {
	return jwk{
		Kty: "RSA", Kid: kid, Use: "sig",
		N: base64.RawURLEncoding.EncodeToString(pub.N.Bytes()),
		E: base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes()),
	}
}

// jwksServer serves a key set the test can change, and counts fetches.
type jwksServer struct {
	srv  *httptest.Server
	mu   sync.Mutex
	set  []jwk
	raw  []byte // when set, served instead of set
	fail bool
	hits int
}

func newJWKS(t *testing.T, set ...jwk) *jwksServer {
	t.Helper()
	j := &jwksServer{set: set}
	j.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		j.mu.Lock()
		defer j.mu.Unlock()
		j.hits++
		if j.fail {
			http.Error(w, "down", http.StatusServiceUnavailable)
			return
		}
		if j.raw != nil {
			_, _ = w.Write(j.raw)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string][]jwk{"keys": j.set})
	}))
	t.Cleanup(j.srv.Close)
	return j
}

func (j *jwksServer) fetches() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.hits
}

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func testVerifier(j *jwksServer, c *clock) *Verifier {
	v := New("us-east-1", testPool, testClient)
	v.jwksURL = j.srv.URL
	v.now = c.now
	return v
}

func goodClaims(now time.Time) jwt.MapClaims {
	return jwt.MapClaims{
		"sub":              "sub-1",
		"iss":              testIssuer,
		"aud":              testClient,
		"token_use":        "id",
		"exp":              now.Add(time.Hour).Unix(),
		"iat":              now.Unix(),
		"email":            "owner@example.com",
		"email_verified":   true,
		"cognito:username": "Google_123",
	}
}

func sign(t *testing.T, key *rsa.PrivateKey, kid string, claims jwt.MapClaims) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	tok.Header["kid"] = kid
	s, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func setup(t *testing.T) (*Verifier, *jwksServer, *clock, *rsa.PrivateKey) {
	t.Helper()
	key, _ := keys(t)
	j := newJWKS(t, rsaJWK("k1", &key.PublicKey))
	c := &clock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	return testVerifier(j, c), j, c, key
}

func TestAValidTokenIsAcceptedWithOrWithoutTheBearerScheme(t *testing.T) {
	v, _, c, key := setup(t)
	tok := sign(t, key, "k1", goodClaims(c.t))
	for _, header := range []string{"Bearer " + tok, "bearer " + tok, tok} {
		got, err := v.Verify(context.Background(), header)
		if err != nil {
			t.Fatalf("%.10q...: %v", header, err)
		}
		want := Claims{Sub: "sub-1", Email: "owner@example.com", EmailVerified: true, CognitoUsername: "Google_123"}
		if got != want {
			t.Errorf("claims = %+v, want %+v", got, want)
		}
	}
}

func TestForgedAndWrongTokensAreRefused(t *testing.T) {
	v, _, c, key := setup(t)
	_, other := keys(t)
	pubDER, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	pubPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER})

	with := func(edit func(jwt.MapClaims)) jwt.MapClaims {
		m := goodClaims(c.t)
		edit(m)
		return m
	}
	none, err := jwt.NewWithClaims(jwt.SigningMethodNone, goodClaims(c.t)).SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatal(err)
	}
	hsTok := jwt.NewWithClaims(jwt.SigningMethodHS256, goodClaims(c.t))
	hsTok.Header["kid"] = "k1"
	hs, err := hsTok.SignedString(pubPEM)
	if err != nil {
		t.Fatal(err)
	}

	cases := map[string]string{
		"alg none":                 "Bearer " + none,
		"HS256 with the public key": "Bearer " + hs,
		"signed by another key":    "Bearer " + sign(t, other, "k1", goodClaims(c.t)),
		"wrong issuer":             "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["iss"] = "https://attacker.example.com" })),
		"wrong audience":           "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["aud"] = "someone-else" })),
		"extra audience":           "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["aud"] = []string{testClient, "someone-else"} })),
		"access token":             "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["token_use"] = "access" })),
		"no exp":                   "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { delete(m, "exp") })),
		"expired beyond leeway":    "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["exp"] = c.t.Add(-31 * time.Second).Unix() })),
		"no sub":                   "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { delete(m, "sub") })),
		"no kid":                   "Bearer " + sign(t, key, "", goodClaims(c.t)),
		"empty header":             "",
		"basic scheme":             "Basic dXNlcjpwYXNz",
		"garbage":                  "Bearer not-a-token",
	}
	for name, header := range cases {
		_, err := v.Verify(context.Background(), header)
		if !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: err = %v, want ErrInvalid", name, err)
		}
	}
}

func TestExpiryAllowsThirtySecondsOfClockDrift(t *testing.T) {
	v, _, c, key := setup(t)
	m := goodClaims(c.t)
	m["exp"] = c.t.Add(-29 * time.Second).Unix()
	if _, err := v.Verify(context.Background(), sign(t, key, "k1", m)); err != nil {
		t.Errorf("a token 29s past exp was refused: %v", err)
	}
}

func TestEmailVerifiedAcceptsOnlyTrueOrTheStringTrue(t *testing.T) {
	v, _, c, key := setup(t)
	for raw, want := range map[any]bool{true: true, "true": true, false: false, "false": false, "yes": false, 1: false} {
		m := goodClaims(c.t)
		m["email_verified"] = raw
		got, err := v.Verify(context.Background(), sign(t, key, "k1", m))
		if err != nil {
			t.Fatalf("%v: %v", raw, err)
		}
		if got.EmailVerified != want {
			t.Errorf("email_verified %#v -> %v, want %v", raw, got.EmailVerified, want)
		}
	}
	m := goodClaims(c.t)
	delete(m, "email_verified")
	if got, _ := v.Verify(context.Background(), sign(t, key, "k1", m)); got.EmailVerified {
		t.Error("an absent email_verified was treated as verified")
	}
}

func TestAnUnknownKeyIDRefetchesAtMostEveryFiveMinutes(t *testing.T) {
	v, j, c, key := setup(t)
	_, rotated := keys(t)
	ctx := context.Background()

	if _, err := v.Verify(ctx, sign(t, key, "k1", goodClaims(c.t))); err != nil {
		t.Fatal(err)
	}
	if j.fetches() != 1 {
		t.Fatalf("fetches = %d after first use, want 1", j.fetches())
	}

	// Cognito rotates, but inside the window the verifier does not ask again.
	j.mu.Lock()
	j.set = append(j.set, rsaJWK("k2", &rotated.PublicKey))
	j.mu.Unlock()
	rotatedTok := sign(t, rotated, "k2", goodClaims(c.t))
	if _, err := v.Verify(ctx, rotatedTok); !errors.Is(err, ErrInvalid) {
		t.Fatalf("unknown kid inside the window: err = %v, want ErrInvalid", err)
	}
	if _, err := v.Verify(ctx, sign(t, key, "junk", goodClaims(c.t))); !errors.Is(err, ErrInvalid) {
		t.Fatalf("second unknown kid: err = %v, want ErrInvalid", err)
	}
	if j.fetches() != 1 {
		t.Fatalf("fetches = %d inside the window, want 1", j.fetches())
	}

	// Five minutes on, one refetch picks up the rotated key.
	c.t = c.t.Add(5 * time.Minute)
	if _, err := v.Verify(ctx, rotatedTok); err != nil {
		t.Fatalf("rotated key after the window: %v", err)
	}
	if j.fetches() != 2 {
		t.Fatalf("fetches = %d after the window, want 2", j.fetches())
	}
	if _, err := v.Verify(ctx, sign(t, key, "junk", goodClaims(c.t))); !errors.Is(err, ErrInvalid) {
		t.Fatalf("junk kid right after a refetch: err = %v, want ErrInvalid", err)
	}
	if j.fetches() != 2 {
		t.Errorf("fetches = %d, want 2: a junk kid right after a refetch must not fetch again", j.fetches())
	}
}

func TestAKeyFetchFailureIsUnavailableNotInvalid(t *testing.T) {
	v, j, c, key := setup(t)
	tok := sign(t, key, "k1", goodClaims(c.t))
	j.mu.Lock()
	j.fail = true
	j.mu.Unlock()
	if _, err := v.Verify(context.Background(), tok); !errors.Is(err, ErrUnavailable) || errors.Is(err, ErrInvalid) {
		t.Fatalf("err = %v, want ErrUnavailable only", err)
	}
	j.mu.Lock()
	j.fail = false
	j.mu.Unlock()
	if _, err := v.Verify(context.Background(), tok); err != nil {
		t.Errorf("after Cognito recovered: %v", err)
	}
}

func TestAnOversizedKeySetIsUnavailable(t *testing.T) {
	v, j, c, key := setup(t)
	j.mu.Lock()
	j.raw = []byte(`{"keys":[],"pad":"` + strings.Repeat("a", maxJWKSBytes) + `"}`)
	j.mu.Unlock()
	if _, err := v.Verify(context.Background(), sign(t, key, "k1", goodClaims(c.t))); !errors.Is(err, ErrUnavailable) {
		t.Errorf("err = %v, want ErrUnavailable", err)
	}
}

func TestOnlyRSASigningKeysOfAtLeast2048BitsAreKept(t *testing.T) {
	key, _ := keys(t)
	small, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatal(err)
	}
	enc := rsaJWK("enc", &key.PublicKey)
	enc.Use = "enc"
	noUse := rsaJWK("nouse", &key.PublicKey)
	noUse.Use = ""
	body, _ := json.Marshal(map[string][]jwk{"keys": {
		rsaJWK("good", &key.PublicKey),
		noUse,
		enc,
		{Kty: "EC", Kid: "ec"},
		rsaJWK("small", &small.PublicKey),
	}})
	got, err := parseJWKS(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got["good"] == nil || got["nouse"] == nil {
		t.Errorf("kept %v, want exactly good and nouse", mapKeys(got))
	}
	if _, err := parseJWKS([]byte(`{"keys":[{"kty":"EC","kid":"ec"}]}`)); err == nil {
		t.Error("a key set with no usable key parsed without error")
	}
}

func mapKeys(m map[string]*rsa.PublicKey) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
```

- [ ] **Step 3: Write the failing `Authenticate` tests**

`cloud/internal/idtoken/authenticate_test.go`:

```go
package idtoken

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/events"
)

type tokensFunc func(context.Context, string) (Claims, error)

func (f tokensFunc) Verify(ctx context.Context, h string) (Claims, error) { return f(ctx, h) }

func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func withAuthorizer(r events.APIGatewayV2HTTPRequest) events.APIGatewayV2HTTPRequest {
	r.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{
			Claims: map[string]string{"sub": "forged-owner-sub"},
		},
	}
	return r
}

func TestAuthenticateReadsTheAuthorizationHeaderInAnyCase(t *testing.T) {
	var seen string
	tokens := tokensFunc(func(_ context.Context, h string) (Claims, error) {
		seen = h
		return Claims{Sub: "sub-1"}, nil
	})
	req := events.APIGatewayV2HTTPRequest{Headers: map[string]string{"Authorization": "Bearer abc"}}
	got, err := Authenticate(context.Background(), tokens, req)
	if err != nil || got.Sub != "sub-1" || seen != "Bearer abc" {
		t.Errorf("got %+v, %v; verifier saw %q", got, err, seen)
	}
}

func TestAuthenticateLogsAMismatchWhenTheAuthorizerHadAcceptedTheCall(t *testing.T) {
	buf := captureLog(t)
	tokens := tokensFunc(func(context.Context, string) (Claims, error) {
		return Claims{}, fmt.Errorf("%w: token has invalid issuer", ErrInvalid)
	})
	req := withAuthorizer(events.APIGatewayV2HTTPRequest{
		RouteKey: "GET /api/devices",
		Headers:  map[string]string{"authorization": "Bearer secret-token-value"},
	})
	if _, err := Authenticate(context.Background(), tokens, req); !errors.Is(err, ErrInvalid) {
		t.Fatalf("err = %v, want ErrInvalid", err)
	}
	out := buf.String()
	if !strings.Contains(out, MismatchMessage) || !strings.Contains(out, "GET /api/devices") {
		t.Errorf("log lacks the mismatch line or route: %s", out)
	}
	for _, secret := range []string{"secret-token-value", "forged-owner-sub"} {
		if strings.Contains(out, secret) {
			t.Errorf("log contains %q: %s", secret, out)
		}
	}
}

func TestAuthenticateDoesNotCallAMissingTokenAMismatchWithoutAnAuthorizer(t *testing.T) {
	buf := captureLog(t)
	tokens := tokensFunc(func(context.Context, string) (Claims, error) {
		return Claims{}, fmt.Errorf("%w: no token", ErrInvalid)
	})
	if _, err := Authenticate(context.Background(), tokens, events.APIGatewayV2HTTPRequest{}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("err = %v, want ErrInvalid", err)
	}
	if strings.Contains(buf.String(), MismatchMessage) {
		t.Errorf("logged a mismatch with no authorizer block: %s", buf.String())
	}
}

func TestAuthenticatePassesUnavailableThroughWithoutAMismatch(t *testing.T) {
	buf := captureLog(t)
	tokens := tokensFunc(func(context.Context, string) (Claims, error) {
		return Claims{}, fmt.Errorf("%w: status 503", ErrUnavailable)
	})
	req := withAuthorizer(events.APIGatewayV2HTTPRequest{RouteKey: "GET /api/devices"})
	if _, err := Authenticate(context.Background(), tokens, req); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v, want ErrUnavailable", err)
	}
	if strings.Contains(buf.String(), MismatchMessage) {
		t.Errorf("an outage was logged as a mismatch: %s", buf.String())
	}
}
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd cloud && go test ./internal/idtoken/`
Expected: FAIL to compile (`undefined: New`, `undefined: Authenticate`, and so on).

- [ ] **Step 5: Write `idtoken.go`**

```go
// Package idtoken verifies the Cognito ID tokens the admin site sends, inside
// the function that acts on them.
//
// API Gateway's JWT authorizer already checks each token and hands the
// function its claims in the event. The function cannot tell that event from
// one somebody built by hand and sent with lambda:Invoke, skipping API Gateway
// entirely, so identity is taken from the token itself, checked here, and
// never from the authorizer's claims.
package idtoken

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

var (
	// ErrInvalid means the caller did not present a token this pool issued
	// to this client that is still in date. Answer 401.
	ErrInvalid = errors.New("idtoken: invalid token")
	// ErrUnavailable means the pool's signing keys could not be fetched, so
	// no token can be checked. Answer 503: it fails closed without blaming
	// the caller.
	ErrUnavailable = errors.New("idtoken: signing keys unavailable")
)

const (
	leeway          = 30 * time.Second
	refetchInterval = 5 * time.Minute
	fetchTimeout    = 5 * time.Second
	maxJWKSBytes    = 64 << 10
	minKeyBits      = 2048
)

// Claims is what the handlers may know about a verified caller.
type Claims struct {
	Sub             string
	Email           string
	EmailVerified   bool
	CognitoUsername string
}

// Tokens verifies an Authorization header. Verifier is the real one; tests
// use idtokentest.Fake.
type Tokens interface {
	Verify(ctx context.Context, authorizationHeader string) (Claims, error)
}

// Verifier checks tokens against one user pool and one app client.
type Verifier struct {
	issuer   string
	audience string
	jwksURL  string
	client   *http.Client
	now      func() time.Time

	mu        sync.Mutex
	keys      map[string]*rsa.PublicKey
	fetchedAt time.Time
}

// New returns a Verifier for tokens the pool issues to the client. Keys are
// fetched on first use, not here, so a Cognito outage at cold start does not
// crash-loop the function.
func New(region, userPoolID, clientID string) *Verifier {
	issuer := fmt.Sprintf("https://cognito-idp.%s.amazonaws.com/%s", region, userPoolID)
	return &Verifier{
		issuer:   issuer,
		audience: clientID,
		jwksURL:  issuer + "/.well-known/jwks.json",
		client:   &http.Client{Timeout: fetchTimeout},
		now:      time.Now,
	}
}

type cognitoClaims struct {
	jwt.RegisteredClaims
	TokenUse        string   `json:"token_use"`
	Email           string   `json:"email"`
	EmailVerified   flexBool `json:"email_verified"`
	CognitoUsername string   `json:"cognito:username"`
}

// flexBool is true only for JSON true or the string "true". Cognito writes a
// boolean for its own attributes, but an attribute mapped from a federated
// identity provider can arrive as a string.
type flexBool bool

func (b *flexBool) UnmarshalJSON(data []byte) error {
	s := string(data)
	*b = flexBool(s == "true" || s == `"true"`)
	return nil
}

// Verify accepts "Bearer <token>" (any case) or a bare token, as API Gateway
// does. Error messages name the check that failed, never the token or its
// claims.
func (v *Verifier) Verify(ctx context.Context, header string) (Claims, error) {
	raw := strings.TrimSpace(header)
	if scheme, rest, ok := strings.Cut(raw, " "); ok {
		if !strings.EqualFold(scheme, "Bearer") {
			return Claims{}, fmt.Errorf("%w: unsupported authorization scheme", ErrInvalid)
		}
		raw = strings.TrimSpace(rest)
	}
	if raw == "" {
		return Claims{}, fmt.Errorf("%w: no token", ErrInvalid)
	}

	var unavailable error
	parser := jwt.NewParser(
		// Checked before the key is looked up, so "none" and an HMAC token
		// signed with the public key never reach a verification step.
		jwt.WithValidMethods([]string{"RS256"}),
		jwt.WithIssuer(v.issuer),
		jwt.WithAudience(v.audience),
		jwt.WithExpirationRequired(),
		jwt.WithLeeway(leeway),
		jwt.WithTimeFunc(v.now),
	)
	claims := &cognitoClaims{}
	_, err := parser.ParseWithClaims(raw, claims, func(t *jwt.Token) (any, error) {
		kid, _ := t.Header["kid"].(string)
		if kid == "" {
			return nil, errors.New("no key id")
		}
		key, err := v.keyFor(ctx, kid)
		if errors.Is(err, ErrUnavailable) {
			unavailable = err
		}
		return key, err
	})
	if unavailable != nil {
		return Claims{}, unavailable
	}
	if err != nil {
		return Claims{}, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	// WithAudience accepts a token whose aud merely includes the client.
	// Cognito's ID tokens carry exactly one.
	if len(claims.Audience) != 1 {
		return Claims{}, fmt.Errorf("%w: audience is not exactly the site client", ErrInvalid)
	}
	if claims.TokenUse != "id" {
		return Claims{}, fmt.Errorf("%w: not an ID token", ErrInvalid)
	}
	if claims.Subject == "" {
		return Claims{}, fmt.Errorf("%w: no subject", ErrInvalid)
	}
	return Claims{
		Sub:             claims.Subject,
		Email:           claims.Email,
		EmailVerified:   bool(claims.EmailVerified),
		CognitoUsername: claims.CognitoUsername,
	}, nil
}

// keyFor returns the signing key with this ID. Until the first successful
// fetch, every call fetches. After that, an unknown ID fetches again at most
// once per refetchInterval, so a stream of junk key IDs costs Cognito one
// request per interval per execution environment.
func (v *Verifier) keyFor(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.keys == nil {
		if err := v.fetchLocked(ctx); err != nil {
			return nil, err
		}
	}
	if k, ok := v.keys[kid]; ok {
		return k, nil
	}
	if v.now().Sub(v.fetchedAt) >= refetchInterval {
		// A failed refresh keeps the keys already held; the token is refused
		// below either way.
		if err := v.fetchLocked(ctx); err != nil {
			slog.Error("refreshing signing keys", "err", err)
		}
		if k, ok := v.keys[kid]; ok {
			return k, nil
		}
	}
	return nil, fmt.Errorf("%w: unknown key id", ErrInvalid)
}

func (v *Verifier) fetchLocked(ctx context.Context) error {
	v.fetchedAt = v.now()
	ctx, cancel := context.WithTimeout(ctx, fetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, v.jwksURL, nil)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	resp, err := v.client.Do(req)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: status %d", ErrUnavailable, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxJWKSBytes+1))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	if len(body) > maxJWKSBytes {
		return fmt.Errorf("%w: key set larger than %d bytes", ErrUnavailable, maxJWKSBytes)
	}
	keys, err := parseJWKS(body)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	v.keys = keys
	return nil
}

// parseJWKS keeps RSA signing keys of at least minKeyBits and ignores the
// rest, rather than failing on a key type it does not use.
func parseJWKS(body []byte) (map[string]*rsa.PublicKey, error) {
	var set struct {
		Keys []struct {
			Kty string `json:"kty"`
			Kid string `json:"kid"`
			Use string `json:"use"`
			N   string `json:"n"`
			E   string `json:"e"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, err
	}
	keys := map[string]*rsa.PublicKey{}
	for _, k := range set.Keys {
		if k.Kty != "RSA" || k.Kid == "" || (k.Use != "" && k.Use != "sig") {
			continue
		}
		n, err := base64.RawURLEncoding.DecodeString(k.N)
		if err != nil {
			continue
		}
		e, err := base64.RawURLEncoding.DecodeString(k.E)
		if err != nil || len(e) == 0 || len(e) > 4 {
			continue
		}
		pub := &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(new(big.Int).SetBytes(e).Int64())}
		if pub.N.BitLen() < minKeyBits || pub.E < 3 {
			continue
		}
		keys[k.Kid] = pub
	}
	if len(keys) == 0 {
		return nil, errors.New("no usable signing keys")
	}
	return keys, nil
}
```

- [ ] **Step 6: Write `authenticate.go`**

```go
package idtoken

import (
	"context"
	"errors"
	"log/slog"
	"strings"

	"github.com/aws/aws-lambda-go/events"
)

// MismatchMessage is logged when API Gateway's authorizer accepted a request
// whose token Verify refuses. On the real path both check the same token
// against the same issuer and audience, so this is what a hand-built event
// carrying authorizer claims looks like. The scoreboard-token-mismatch metric
// filters (terraform/admin.tf) match this exact text.
const MismatchMessage = "token rejected after authorizer accepted"

// Authenticate verifies the request's Authorization header and returns the
// caller. It never reads identity from the authorizer block; it only notes
// whether one was present, to log a mismatch.
func Authenticate(ctx context.Context, t Tokens, req events.APIGatewayV2HTTPRequest) (Claims, error) {
	header := ""
	for name, value := range req.Headers {
		if strings.EqualFold(name, "authorization") {
			header = value
			break
		}
	}
	claims, err := t.Verify(ctx, header)
	if err == nil {
		return claims, nil
	}
	route := req.RequestContext.RouteKey
	if route == "" {
		route = req.RouteKey
	}
	if errors.Is(err, ErrUnavailable) {
		slog.Error("token verification unavailable", "route", route, "err", err)
		return Claims{}, err
	}
	if req.RequestContext.Authorizer != nil && req.RequestContext.Authorizer.JWT != nil {
		slog.Warn(MismatchMessage, "route", route, "check", err.Error())
	}
	return Claims{}, err
}
```

- [ ] **Step 7: Write the test fake**

`cloud/internal/idtoken/idtokentest/fake.go`:

```go
// Package idtokentest is a fake token verifier for handler tests. It accepts
// tokens nobody signed, so production code must never import it: the site's
// invoke-config tripwire fails the build if a non-test file does.
package idtokentest

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"strings"

	"hockeytrack-scoreboard/internal/idtoken"
)

const prefix = "Bearer fake:"

// Header returns an Authorization header that Fake accepts as carrying c.
func Header(c idtoken.Claims) string {
	q := url.Values{}
	q.Set("sub", c.Sub)
	q.Set("email", c.Email)
	q.Set("email_verified", strconv.FormatBool(c.EmailVerified))
	q.Set("cognito:username", c.CognitoUsername)
	return prefix + q.Encode()
}

// Fake accepts headers made by Header. With Unavailable set, every call
// fails as if Cognito's keys could not be fetched.
type Fake struct{ Unavailable bool }

func (f Fake) Verify(_ context.Context, header string) (idtoken.Claims, error) {
	if f.Unavailable {
		return idtoken.Claims{}, fmt.Errorf("%w: fake outage", idtoken.ErrUnavailable)
	}
	rest, ok := strings.CutPrefix(header, prefix)
	if !ok {
		return idtoken.Claims{}, fmt.Errorf("%w: not a fake token", idtoken.ErrInvalid)
	}
	q, err := url.ParseQuery(rest)
	if err != nil || q.Get("sub") == "" {
		return idtoken.Claims{}, fmt.Errorf("%w: fake token without a subject", idtoken.ErrInvalid)
	}
	return idtoken.Claims{
		Sub:             q.Get("sub"),
		Email:           q.Get("email"),
		EmailVerified:   q.Get("email_verified") == "true",
		CognitoUsername: q.Get("cognito:username"),
	}, nil
}
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd cloud && go mod tidy && go vet ./internal/idtoken/... && go test ./internal/idtoken/...`
Expected: PASS. If a golang-jwt option name differs in the fetched version (for example `WithAudience` being variadic), adapt the call without changing the behavior, and say so in the report.

- [ ] **Step 9: Align the spec with three implementation details**

Edit `docs/superpowers/specs/2026-09-15-direct-invoke-design.md`:
- **§3.1:** at the end of the JWKS bullet's last sub-bullet ("Keys that are not `kty: RSA` with `use: sig` (or no `use`) are ignored."), append ` So are RSA keys shorter than 2048 bits.`
- **§3.3, first bullet:** replace `the handler logs at warn level` with `` `idtoken.Authenticate`, which both handlers call, logs at warn level ``.
- **§6, Terraform tripwires:** replace `The metric filter's quoted term appears verbatim in both handlers' source.` with `` The metric filters' quoted term is `idtoken.MismatchMessage` verbatim, both handlers call `idtoken.Authenticate`, and no production code imports the test fake `internal/idtoken/idtokentest`. ``

- [ ] **Step 10: Commit**

```bash
git add cloud/go.mod cloud/go.sum cloud/internal/idtoken docs/superpowers/specs/2026-09-15-direct-invoke-design.md
git commit -m "security: verify Cognito ID tokens inside the function

Adds internal/idtoken on github.com/golang-jwt/jwt/v5 <version>: RS256 only,
issuer, exact audience, token_use id, exp with 30s leeway, JWKS cached with
a rate-limited refetch. Authenticate logs the mismatch line the alarm will
match. idtokentest holds the fake verifier, for tests only.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 2: The handlers authenticate from the token

**Files:**
- Modify: `cloud/cmd/api/handler.go` (the `subject` function, imports, `Handler`, start of `Handle`)
- Modify: `cloud/cmd/api/main.go`
- Modify: `cloud/cmd/api/handler_test.go`
- Modify: `cloud/cmd/enroll/handler.go` (the `subject` function, imports, `Handler`, `ownerMatches`, start of `claim`)
- Modify: `cloud/cmd/enroll/main.go`
- Modify: `cloud/cmd/enroll/handler_test.go`

**Interfaces:**
- **Consumes, from Task 1:** `idtoken.Authenticate`, `idtoken.Tokens`, `idtoken.Claims`, `idtoken.ErrUnavailable`, `idtoken.New`, `idtokentest.Fake`, `idtokentest.Header`.
- **Produces, for Task 3:**
  - `Handler.Tokens idtoken.Tokens` in both commands.
  - Both `main.go` files read `USER_POOL_ID` and `APP_CLIENT_ID`.
  - Neither command's non-test source contains `JWT.Claims`. Comments must not contain that string either, because Task 3's tripwire does not strip comments.

- [ ] **Step 1: Point the api tests at tokens, and add the failing ones**

In `cloud/cmd/api/handler_test.go`:

Add imports `"bytes"`, `"log/slog"`, `"hockeytrack-scoreboard/internal/idtoken"` and `"hockeytrack-scoreboard/internal/idtoken/idtokentest"`.

Replace `req` and `handlerWith` with:

```go
// req builds a request the way API Gateway delivers a signed-in call: the
// token in the header and, because the authorizer accepted it, its claims in
// the authorizer block. The handler must believe only the header.
func req(method, route, sub, body string, params map[string]string) events.APIGatewayV2HTTPRequest {
	r := events.APIGatewayV2HTTPRequest{Body: body, PathParameters: params, Headers: map[string]string{}}
	r.RequestContext.HTTP.Method = method
	r.RequestContext.RouteKey = route
	r.RouteKey = route
	if sub != "" {
		r.Headers["authorization"] = idtokentest.Header(idtoken.Claims{Sub: sub})
		r.RequestContext.Authorizer = authorizer(sub)
	}
	return r
}

func authorizer(sub string) *events.APIGatewayV2HTTPRequestContextAuthorizerDescription {
	return &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{Claims: map[string]string{"sub": sub}},
	}
}

func handlerWith(t *testing.T) (*Handler, *devices.Fake, *iotpub.Fake) {
	t.Helper()
	st, pub := devices.NewFake(), &iotpub.Fake{}
	_ = st.Register(context.Background(), "scoreboard-7qf2")
	return &Handler{Store: st, Pub: pub, Tokens: idtokentest.Fake{}}, st, pub
}
```

Append:

```go
func TestForgedAuthorizerClaimsWithoutATokenAreRefusedAndLogged(t *testing.T) {
	// A hand-built event sent straight to the function: the owner's sub in
	// the authorizer block, and no token.
	h, st, pub := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })

	r := req("PUT", "PUT /api/devices/{thing}/game", "", `{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"})
	r.RequestContext.Authorizer = authorizer("sub-a")
	res, err := h.Handle(ctx, r)
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 401 {
		t.Errorf("status = %d, want 401", res.StatusCode)
	}
	if len(pub.Messages) != 0 {
		t.Error("a forged event published a config")
	}
	if !strings.Contains(buf.String(), idtoken.MismatchMessage) {
		t.Errorf("no mismatch line logged: %s", buf.String())
	}
}

func TestIdentityComesFromTheTokenNotTheAuthorizerBlock(t *testing.T) {
	h, st, pub := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")

	r := req("PUT", "PUT /api/devices/{thing}/game", "sub-b", `{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"})
	r.RequestContext.Authorizer = authorizer("sub-a")
	res, _ := h.Handle(ctx, r)
	if res.StatusCode != 404 {
		t.Errorf("status = %d, want 404: the token says sub-b, who does not own the panel", res.StatusCode)
	}
	if len(pub.Messages) != 0 {
		t.Error("published on the authorizer block's say-so")
	}
}

func TestAValidTokenWithoutAnAuthorizerBlockIsServed(t *testing.T) {
	// A direct invoke carrying a genuine token. Verification accepts it by
	// design; HockeyTrack's section 12 rule is what sees the invoke.
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	r := req("GET", "GET /api/devices", "sub-a", "", nil)
	r.RequestContext.Authorizer = nil
	res, _ := h.Handle(ctx, r)
	if res.StatusCode != 200 {
		t.Errorf("status = %d, want 200", res.StatusCode)
	}
}

func TestUnavailableSigningKeysAre503(t *testing.T) {
	h, _, _ := handlerWith(t)
	h.Tokens = idtokentest.Fake{Unavailable: true}
	res, _ := h.Handle(context.Background(), req("GET", "GET /api/devices", "sub-a", "", nil))
	if res.StatusCode != 503 || !strings.Contains(res.Body, "sign-in check unavailable") {
		t.Errorf("got %d %s, want 503 sign-in check unavailable", res.StatusCode, res.Body)
	}
}
```

- [ ] **Step 2: Run the api tests to verify they fail**

Run: `cd cloud && go test ./cmd/api/`
Expected: FAIL to compile, with `unknown field Tokens in struct literal`.

- [ ] **Step 3: Change the api handler and main**

In `cloud/cmd/api/handler.go`:
- Add `"errors"` and `"hockeytrack-scoreboard/internal/idtoken"` to the imports.
- Add the field `Tokens idtoken.Tokens` to `Handler`.
- Delete the `subject` function.
- Replace the first four lines of `Handle`:

```go
	sub := subject(req)
	if sub == "" {
		return fail(401, "unauthenticated")
	}
```

with:

```go
	caller, err := idtoken.Authenticate(ctx, h.Tokens, req)
	if errors.Is(err, idtoken.ErrUnavailable) {
		return fail(503, "sign-in check unavailable")
	}
	if err != nil {
		return fail(401, "unauthenticated")
	}
	sub := caller.Sub
```

The existing `rawBody, err := decodeBody(req)` line below still compiles, because `rawBody` is new; leave it.

Update the package comment in `cloud/cmd/api/main.go`, replacing the sentence "The authorizer proves the caller signed in; this process decides what they may touch." with:

```go
// with a Cognito JWT authorizer. This process verifies the caller's ID token
// again itself (internal/idtoken), because an event can reach it without
// passing the authorizer, and decides what they may touch.
```

In `main()`, after the existing environment check, add:

```go
	pool, client := os.Getenv("USER_POOL_ID"), os.Getenv("APP_CLIENT_ID")
	if pool == "" || client == "" {
		slog.Error("USER_POOL_ID and APP_CLIENT_ID are required")
		os.Exit(1)
	}
```

Then add `Tokens: idtoken.New(cfg.Region, pool, client),` to the `Handler` literal, and `"hockeytrack-scoreboard/internal/idtoken"` to the imports.

- [ ] **Step 4: Run the api tests to verify they pass**

Run: `cd cloud && go vet ./cmd/api/ && go test ./cmd/api/`
Expected: PASS: the 6 existing tests plus 4 new.

- [ ] **Step 5: Point the enroll tests at tokens, and add the failing ones**

In `cloud/cmd/enroll/handler_test.go`:

Add imports `"bytes"`, `"log/slog"`, `"hockeytrack-scoreboard/internal/idtoken"` and `"hockeytrack-scoreboard/internal/idtoken/idtokentest"` (keep `strings` if already imported; add it if not).

Add `Tokens: idtokentest.Fake{},` to all three `&Handler{` literals: `newHandler`, and the two collision tests near the end of the file.

Replace `claimAs` with:

```go
// claimAs lets a test supply exactly the claims a token would carry, for
// cases that need to control email_verified rather than get it for free. The
// same claims go in the authorizer block, as API Gateway would put them.
func claimAs(h *Handler, code string, claims map[string]string) events.APIGatewayV2HTTPResponse {
	req := post("/api/devices/claim", `{"code":"`+code+`"}`)
	req.Headers = map[string]string{"authorization": idtokentest.Header(idtoken.Claims{
		Sub:             claims["sub"],
		Email:           claims["email"],
		EmailVerified:   claims["email_verified"] == "true",
		CognitoUsername: claims["cognito:username"],
	})}
	req.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{Claims: claims},
	}
	resp, _ := h.Handle(context.Background(), req)
	return resp
}
```

Append:

```go
func TestForgedOwnerClaimsWithoutATokenCannotClaimAPreBoundPanel(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submitFor(t, h, "owner-1@example.com")
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })

	req := post("/api/devices/claim", `{"code":"`+code+`"}`)
	req.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{Claims: map[string]string{
			"sub": "attacker", "email": "owner-1@example.com", "email_verified": "true",
		}},
	}
	resp, _ := h.Handle(context.Background(), req)
	if resp.StatusCode != 401 {
		t.Errorf("forged claim returned %d, want 401", resp.StatusCode)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("forged authorizer claims minted a certificate")
	}
	if !strings.Contains(buf.String(), idtoken.MismatchMessage) {
		t.Errorf("no mismatch line logged: %s", buf.String())
	}
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the real owner's claim returned %d afterwards, want 200", got)
	}
}

func TestTheOwnerHintIsCheckedAgainstTheTokenNotTheAuthorizerBlock(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submitFor(t, h, "owner-1@example.com")
	req := post("/api/devices/claim", `{"code":"`+code+`"}`)
	req.Headers = map[string]string{"authorization": idtokentest.Header(idtoken.Claims{
		Sub: "owner-2", Email: "owner-2@example.com", EmailVerified: true,
	})}
	req.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{Claims: map[string]string{
			"sub": "owner-2", "email": "owner-1@example.com", "email_verified": "true",
		}},
	}
	resp, _ := h.Handle(context.Background(), req)
	if resp.StatusCode != 404 {
		t.Errorf("got %d, want 404: the token names owner-2", resp.StatusCode)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("the authorizer block's email minted a certificate")
	}
}

func TestClaimingWhileSigningKeysAreUnavailableIs503(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submit(t, h)
	h.Tokens = idtokentest.Fake{Unavailable: true}
	if got := claim(h, code, "owner-1").StatusCode; got != 503 {
		t.Errorf("got %d, want 503", got)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("a claim was served without a verified token")
	}
}

func TestTheDeviceRoutesNeverAskForASignInToken(t *testing.T) {
	// POST and GET /api/enroll are the panel's own routes. A Cognito outage
	// must not stop a panel enrolling or polling.
	h, _ := newHandler()
	h.Tokens = idtokentest.Fake{Unavailable: true}
	_, token := submit(t, h)
	if got := collect(h, token).StatusCode; got == 503 {
		t.Error("collect consulted the sign-in verifier")
	}
}
```

- [ ] **Step 6: Run the enroll tests to verify they fail**

Run: `cd cloud && go test ./cmd/enroll/`
Expected: FAIL to compile, with `unknown field Tokens in struct literal`.

- [ ] **Step 7: Change the enroll handler and main**

In `cloud/cmd/enroll/handler.go`:
- Add the import `"hockeytrack-scoreboard/internal/idtoken"`.
- Add the field `Tokens idtoken.Tokens` to `Handler`.
- Delete the `subject` function.

In `claim`, replace:

```go
	sub := subject(req)
	if sub == "" {
		return fail(http.StatusUnauthorized, "unauthenticated")
	}
```

with:

```go
	caller, err := idtoken.Authenticate(ctx, h.Tokens, req)
	if errors.Is(err, idtoken.ErrUnavailable) {
		return fail(http.StatusServiceUnavailable, "sign-in check unavailable")
	}
	if err != nil {
		return fail(http.StatusUnauthorized, "unauthenticated")
	}
	sub := caller.Sub
```

Later `:=` lines that also declare `err` (`p, found, err := ...`) still compile, because each declares a new variable too. Change the call `ownerMatches(p, req)` to `ownerMatches(p, caller)`.

Change `ownerMatches`:
- Signature: `func ownerMatches(p enroll.Pending, caller idtoken.Claims) bool`.
- Delete the `if req.RequestContext.Authorizer == nil ...` block and the `claims := ...` line.
- `claims["cognito:username"]` becomes `caller.CognitoUsername`.
- `claims["email"]` becomes `caller.Email`.
- `claims["email_verified"] == "true"` becomes `caller.EmailVerified`.

Replace the comment paragraph that begins `// The exact string API Gateway's JWT authorizer flattens` (through `// miss is logged rather than left to be diagnosed at hardware-test time.`) with:

```go
	// email_verified is read from the verified token (internal/idtoken), which
	// accepts JSON true or the string "true" and nothing else. A near miss --
	// the hint matches but the flag is not set -- fails closed with the same
	// 404 as every other refusal here, so it is logged rather than left to be
	// diagnosed at hardware-test time.
```

Replace the warn call:

```go
			slog.Warn("owner hint matched an email claim that is not verified",
				"email_verified", claims["email_verified"])
```

with:

```go
			slog.Warn("owner hint matched an email claim that is not verified")
```

Delete the comment line above it that says only the flag's raw form is logged.

In `cloud/cmd/enroll/main.go`, after the existing environment check, add:

```go
	pool, client := os.Getenv("USER_POOL_ID"), os.Getenv("APP_CLIENT_ID")
	if pool == "" || client == "" {
		slog.Error("USER_POOL_ID and APP_CLIENT_ID are required")
		os.Exit(1)
	}
```

Then add `Tokens: idtoken.New(cfg.Region, pool, client),` to the `Handler` literal, and add the import.

- [ ] **Step 8: Run all Go checks**

Run from the repository root: `make test-go`
Expected: PASS for every package. Also run `grep -rn "JWT.Claims" cloud --include=*.go | grep -v _test.go`; expected: no output.

- [ ] **Step 9: Commit**

```bash
git add cloud/cmd/api cloud/cmd/enroll
git commit -m "security: take the caller's identity from the verified token

Both admin functions now authenticate through idtoken.Authenticate and never
read identity from the authorizer block, so an event sent straight to either
function with forged claims is refused (401) and logged. A key fetch failure
fails closed with 503.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 3: Terraform, the mismatch alarm, and tripwires

**Files:**
- Modify: `terraform/admin.tf` (`aws_lambda_function.api` environment; new resources after it)
- Modify: `terraform/enroll.tf` (`aws_lambda_function.enroll` environment)
- Create: `site/tests/invoke-config.test.js`
- Modify: `site/tests/signin-config.test.js` (`alarms >= 13`)

**Interfaces:**
- **Consumes:** `idtoken.MismatchMessage` in `cloud/internal/idtoken/authenticate.go` (Task 1); `idtoken.Authenticate(` and `idtoken.New(` call sites (Task 2).
- **Produces, for Task 5's plan check:** resources `aws_cloudwatch_log_metric_filter.token_mismatch_api`, `aws_cloudwatch_log_metric_filter.token_mismatch_enroll` and `aws_cloudwatch_metric_alarm.token_mismatch` (`scoreboard-token-mismatch`).

- [ ] **Step 1: Write the failing tripwires**

`site/tests/invoke-config.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

// Direct invocation (docs/superpowers/specs/2026-09-15-direct-invoke-design.md).
// These read Terraform and Go as text. They catch somebody quietly putting the
// authorizer's claims back in charge of identity, or unhooking the alarm; they
// do not prove what AWS is running.
const tf = (name) => readFileSync(new URL(`../../terraform/${name}`, import.meta.url), "utf8");
const cloud = new URL("../../cloud/", import.meta.url);
const goFiles = readdirSync(cloud, { recursive: true })
  .filter((f) => f.endsWith(".go"))
  .map((f) => ({ path: f.split("\\").join("/"), text: readFileSync(new URL(f, cloud), "utf8") }));
const production = goFiles.filter((f) => !f.path.endsWith("_test.go"));

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}

const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");
const go = (path) => {
  const f = goFiles.find((g) => g.path === path);
  assert.ok(f, `${path} not found`);
  return f.text;
};

test("both admin functions are told which pool and client to trust", () => {
  for (const [file, name] of [["admin.tf", "api"], ["enroll.tf", "enroll"]]) {
    const fn = code(block(tf(file), `resource "aws_lambda_function" "${name}" {`));
    assert.match(fn, /USER_POOL_ID\s*=\s*aws_cognito_user_pool\.admin\.id/, `${name} lacks USER_POOL_ID`);
    assert.match(fn, /APP_CLIENT_ID\s*=\s*aws_cognito_user_pool_client\.site\.id/, `${name} lacks APP_CLIENT_ID`);
  }
});

test("no production code takes identity from the authorizer's claims", () => {
  for (const { path, text } of production) {
    assert.ok(!text.includes("JWT.Claims"), `${path} reads JWT.Claims`);
  }
});

test("both handlers authenticate through idtoken, and both mains build the real verifier", () => {
  for (const cmd of ["api", "enroll"]) {
    assert.ok(go(`cmd/${cmd}/handler.go`).includes("idtoken.Authenticate("), `${cmd} handler does not call idtoken.Authenticate`);
    assert.ok(go(`cmd/${cmd}/main.go`).includes("idtoken.New("), `${cmd} main does not build idtoken.New`);
  }
});

test("the fake verifier is never linked into a Lambda binary", () => {
  for (const { path, text } of production) {
    if (path.startsWith("internal/idtoken/idtokentest/")) continue;
    assert.ok(!text.includes("internal/idtoken/idtokentest"), `${path} imports the test fake`);
  }
});

test("the mismatch filters match the exact line idtoken logs", () => {
  const m = go("internal/idtoken/authenticate.go").match(/const MismatchMessage = "([^"]+)"/);
  assert.ok(m, "MismatchMessage not found");
  for (const [name, group] of [["token_mismatch_api", "api"], ["token_mismatch_enroll", "enroll"]]) {
    const f = code(block(tf("admin.tf"), `resource "aws_cloudwatch_log_metric_filter" "${name}" {`));
    const p = f.match(/pattern\s*=\s*"((?:[^"\\]|\\.)*)"/);
    assert.ok(p, `${name} has no pattern`);
    assert.equal(p[1], `\\"${m[1]}\\"`, `${name} pattern is not the quoted MismatchMessage`);
    assert.match(f, new RegExp(`log_group_name\\s*=\\s*aws_cloudwatch_log_group\\.${group}\\.name`));
    assert.match(f, /name\s*=\s*"TokenMismatch"/);
    assert.match(f, /namespace\s*=\s*"Scoreboard"/);
  }
});

test("the mismatch alarm pages the security topic on a single occurrence", () => {
  const a = code(block(tf("admin.tf"), 'resource "aws_cloudwatch_metric_alarm" "token_mismatch" {'));
  assert.match(a, /alarm_name\s*=\s*"scoreboard-token-mismatch"/);
  assert.match(a, /metric_name\s*=\s*"TokenMismatch"/);
  assert.match(a, /namespace\s*=\s*"Scoreboard"/);
  assert.match(a, /comparison_operator\s*=\s*"GreaterThanOrEqualToThreshold"/);
  assert.match(a, /threshold\s*=\s*1\b/);
  assert.match(a, /period\s*=\s*300\b/);
  assert.match(a, /statistic\s*=\s*"Sum"/);
  assert.match(a, /treat_missing_data\s*=\s*"notBreaching"/);
  assert.match(a, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
});
```

In `site/tests/signin-config.test.js`, change `alarms >= 13` to `alarms >= 14`.

- [ ] **Step 2: Run the tripwires to verify they fail**

Run: `cd site && node --test tests/invoke-config.test.js tests/signin-config.test.js`
Expected:
- Fails: "told which pool and client", "mismatch filters", "mismatch alarm", and the alarm-count floor.
- Passes already after Task 2: "no production code", "handlers authenticate", "fake never linked".

- [ ] **Step 3: Add the environment variables**

In `terraform/admin.tf`, `aws_lambda_function.api`'s `environment.variables` becomes:

```hcl
    variables = {
      DEVICES_TABLE = aws_dynamodb_table.devices.name
      IOT_ENDPOINT  = "https://${data.aws_iot_endpoint.data.endpoint_address}"
      SCHEDULE_URL  = var.schedule_url
      USER_POOL_ID  = aws_cognito_user_pool.admin.id
      APP_CLIENT_ID = aws_cognito_user_pool_client.site.id
    }
```

In `terraform/enroll.tf`, `aws_lambda_function.enroll`'s `environment.variables` becomes:

```hcl
    variables = {
      ENROLLMENTS_TABLE = aws_dynamodb_table.enrollments.name
      DEVICES_TABLE     = aws_dynamodb_table.devices.name
      IOT_ENDPOINT      = data.aws_iot_endpoint.data.endpoint_address
      DEVICE_POLICY     = aws_iot_policy.device.name
      USER_POOL_ID      = aws_cognito_user_pool.admin.id
      APP_CLIENT_ID     = aws_cognito_user_pool_client.site.id
    }
```

- [ ] **Step 4: Add the filters and alarm**

In `terraform/admin.tf`, directly after `resource "aws_lambda_function" "api"`:

```hcl
# The admin functions verify the caller's ID token themselves
# (cloud/internal/idtoken) instead of believing the claims API Gateway's
# authorizer puts in the event, because anyone allowed lambda:InvokeFunction
# can hand a function an event with any claims in it. When the authorizer
# block is present but the token fails verification, API Gateway accepted a
# token the function refused. On the real path both check the same token
# against the same issuer and audience, so that does not happen: it is what a
# hand-built event looks like. HockeyTrack's section 12 rule sees direct
# invocations through CloudTrail; this alarm sees forged ones without
# CloudTrail at all. The pattern is idtoken.MismatchMessage, quoted, and
# assumes Lambda's default text log format, as the authgate filters do.
resource "aws_cloudwatch_log_metric_filter" "token_mismatch_api" {
  name           = "scoreboard-token-mismatch-api"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "\"token rejected after authorizer accepted\""

  metric_transformation {
    name      = "TokenMismatch"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "token_mismatch_enroll" {
  name           = "scoreboard-token-mismatch-enroll"
  log_group_name = aws_cloudwatch_log_group.enroll.name
  pattern        = "\"token rejected after authorizer accepted\""

  metric_transformation {
    name      = "TokenMismatch"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "token_mismatch" {
  alarm_name          = "scoreboard-token-mismatch"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "TokenMismatch"
  alarm_description   = <<-EOT
    scoreboard-api or scoreboard-enroll was handed an event whose authorizer
    block said API Gateway had accepted a token, but the token failed the
    function's own verification. API Gateway does not send that. Assume
    someone invoked the function directly with forged claims. The request was
    refused. Find the caller in CloudTrail (HockeyTrack's section 12 alert
    names them) and follow HockeyTrack's docs/threat-model.md, section 7.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}
```

- [ ] **Step 5: Run everything**

Run:
```bash
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
make test
```
Expected: `fmt` is silent, `validate` prints "Success!", and `make test` passes (govulncheck, Go, Python, JS).

- [ ] **Step 6: Commit**

```bash
git add terraform/admin.tf terraform/enroll.tf site/tests/invoke-config.test.js site/tests/signin-config.test.js
git commit -m "security: page when a function refuses a token API Gateway accepted

USER_POOL_ID and APP_CLIENT_ID for both admin functions; a TokenMismatch
metric filter on each log group and the scoreboard-token-mismatch alarm on
the security topic. Tripwires keep identity off the authorizer claims and
the test fake out of the binaries.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 4: HockeyTrack: log the invocations, keep sections 10 and 11 quiet

**Repository:** `/home/jay/projects/hockeytrack`. First run `git switch -c scoreboard-invoke-detection` from an up-to-date `main`.

**Files:**
- Modify: `terraform/cloudtrail.tf` (after the `dynamic "event_selector"` block in `aws_cloudtrail.account`; new data source)
- Modify: `terraform/security-alarms.tf` (section 10 comment and pattern, section 11 comment and pattern)
- Modify: `docs/threat-model.md` (§4, the sign-in and admin API paragraphs)

**Interfaces:**
- **Produces, for Task 7:** `data.aws_lambda_function.scoreboard_admin_path`, keyed by `"scoreboard-api"`, `"scoreboard-enroll"` and `"scoreboard-authgate"`, with `.arn` as the unqualified ARN.

- [ ] **Step 1: Add the lookups and the selector**

In `terraform/cloudtrail.tf`, directly after the `dynamic "event_selector" { ... }` block inside `aws_cloudtrail.account`:

```hcl
  # Invocations of the three functions that decide who may use the scoreboard
  # admin site: its API, enrollment, and the sign-in gate. Invoke is a data
  # event, so without this the trail cannot show anyone calling them directly
  # with a hand-built event. Lambda's data events are invocations only, so
  # "All" adds no read volume and removes any dependence on how Lambda
  # classifies Invoke. Management events stay with the selector above;
  # CloudTrail does not log them twice. security-alarms.tf section 12 pages on
  # any of these invocations API Gateway or Cognito did not make, and sections
  # 10 and 11 ignore them by eventCategory.
  event_selector {
    read_write_type           = "All"
    include_management_events = false
    data_resource {
      type   = "AWS::Lambda::Function"
      values = [for f in data.aws_lambda_function.scoreboard_admin_path : f.arn]
    }
  }
```

At the end of `terraform/cloudtrail.tf`:

```hcl
# The scoreboard functions whose invocations the trail logs and section 12
# watches. Looked up by name, like section 10's pool and section 11's API, so a
# renamed or deleted function fails this plan instead of silently logging
# nothing -- at the same cost: this repository's plan fails until the
# scoreboard's functions exist again.
data "aws_lambda_function" "scoreboard_admin_path" {
  for_each      = toset(["scoreboard-api", "scoreboard-enroll", "scoreboard-authgate"])
  function_name = each.key
}
```

- [ ] **Step 2: Guard section 10**

In `terraform/security-alarms.tf`, `local.scoreboard_signin_pattern`, add a line after `"eventSource" = [...]`:

```hcl
      "eventCategory" = ["Management"]
```

Keep `terraform fmt`'s alignment. In the section 10 comment, replace exactly:

```
# changes to it. That leaves the gate's own Invoke, once per sign-in as the
# pre sign-up and pre token generation triggers fire: functionName would match
# it exactly like a Lambda write, and every sign-in would page, if any trail
# in this account ever logged Lambda data events. It does not today -- the
# account's one trail, hockeytrack-account, logs S3 object data events only
# (checked 2026-09-14) -- but that is a property of the trail, not of this
# rule. Plans stay silent not because readOnly [false] blocks them but because
```

with:

```
# changes to it. That leaves the gate's own Invoke, once per sign-in as the
# pre sign-up and pre token generation triggers fire. The trail logs it, for
# this function and the admin API's two (cloudtrail.tf, since 2026-09-15), and
# functionName would match it exactly like a Lambda write, so every sign-in
# would page. "eventCategory" = ["Management"] is what stops that: an Invoke
# record's category is Data. Section 12 is the rule that watches invocations,
# and pages only on one Cognito did not make. Plans stay silent not because
# readOnly [false] blocks them but because
```

- [ ] **Step 3: Guard section 11**

In `local.scoreboard_api_pattern`, add `"eventCategory" = ["Management"]` after `"eventSource"` in the same way. In section 11's comment, replace the whole first "does not see" bullet, from `#   - Direct invocation. Both handlers read the caller's identity only from the` through `#     Gateway, not merely logged.`, with:

```
#   - Invocations. API Gateway's own calls to these functions are Invoke
#     records naming them, which the trail now logs (cloudtrail.tf), so this
#     rule ignores data events by eventCategory or it would page on every
#     admin-API request. Direct invocation with forged claims is closed in two
#     other places: both handlers verify the caller's ID token themselves
#     (the scoreboard's cloud/internal/idtoken) rather than trusting the
#     event's authorizer claims, and section 12 pages on any invocation API
#     Gateway did not make.
```

Leave the rest of the list in place, with "Deleting or shortening the logs..." becoming the first remaining item. Also change `# What it does not see, the most important first:` to `# What it does not see, the most important first, after one thing it ignores:`.

- [ ] **Step 4: Update the threat model's §4 paragraphs**

In `docs/threat-model.md`, replace exactly:

```
page: the per-sign-in Cognito events carry no request parameters to match, and
the gate's own invocation would, but the account's one trail does not log
Lambda data events today. The scoreboard repository separately alarms on the
```

with:

```
page: the per-sign-in Cognito events carry no request parameters to match, and
the gate's own invocations, which the trail now logs, are data events this rule
ignores; a third rule, below, watches those. The scoreboard repository separately alarms on the
```

Then replace exactly:

```
scoreboard's resources, because the account runs three other HTTP APIs. Its
largest blind spot needs no write at all. The functions read identity only
from the claims in the event they are handed, so anyone in the account allowed
`lambda:InvokeFunction` on either one can invoke it directly with forged claims,
skipping API Gateway and the authorizer, and the trail does not log Lambda
invocations. What else it does not see is named in its comment, and includes
```

with:

```
scoreboard's resources, because the account runs three other HTTP APIs. It
ignores invocations, which the next paragraph covers. What it does not see is
named in its comment, and includes
```

Rewrap both paragraphs to the file's line width if the result reads unevenly. Change no other wording.

- [ ] **Step 5: Validate**

Run:
```bash
cd /home/jay/projects/hockeytrack
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```
Expected: silent `fmt`, "Success!". Do not run `plan`; the controller does that in Task 6.

- [ ] **Step 6: Commit**

```bash
git add terraform/cloudtrail.tf terraform/security-alarms.tf docs/threat-model.md
git commit -m "security: log invocations of the scoreboard's admin-path functions

A second trail event selector records Lambda data events for scoreboard-api,
scoreboard-enroll and scoreboard-authgate. Sections 10 and 11 now match
management events only, so API Gateway's and Cognito's own invocations do not
page on every request and sign-in.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 5: Scoreboard deploy and proof (controller and user; no subagent)

- [ ] **Step 1: Plan**

```bash
cd /home/jay/projects/hockeytrack-scoreboard
export PATH=$HOME/.local/share/go/bin:$PATH
make test && make build
terraform -chdir=terraform plan -out=<scratchpad>/direct-invoke-1.tfplan
terraform -chdir=terraform show -no-color <scratchpad>/direct-invoke-1.tfplan | grep -E "will be|must be|Plan:"
```

Expected: exactly `aws_lambda_function.api` and `aws_lambda_function.enroll` updated in place (code hash plus two environment variables), and three created: `token_mismatch_api`, `token_mismatch_enroll`, `token_mismatch`. That is `Plan: 3 to add, 2 to change, 0 to destroy.` Any other change stops the task until it is explained.

- [ ] **Step 2: The user applies**

Ask the user to run: `! terraform -chdir=/home/jay/projects/hockeytrack-scoreboard/terraform apply <scratchpad>/direct-invoke-1.tfplan`

Expected emails, from HockeyTrack's existing rules:
- section 11 for the two functions' `UpdateFunctionCode` and `UpdateFunctionConfiguration`;
- alerting-modification for `PutMetricAlarm` on `scoreboard-token-mismatch`.

Record which arrived.

- [ ] **Step 3: Real sign-in still works**

The user signs in to the admin site and loads the panel list. Expected: the panels list as before. If it fails, check `/aws/lambda/scoreboard-api` for `token verification unavailable` or a 401 cause. The rollback is `git switch main && make build` plus a plan and apply.

- [ ] **Step 4: Forged claims are refused and page**

Write `<scratchpad>/forged-api.json`:

```json
{"version":"2.0","routeKey":"GET /api/devices","rawPath":"/api/devices","headers":{"authorization":"Bearer not-a-token"},"requestContext":{"routeKey":"GET /api/devices","http":{"method":"GET","path":"/api/devices"},"authorizer":{"jwt":{"claims":{"sub":"forged-sub"},"scopes":null}}},"isBase64Encoded":false}
```

and `<scratchpad>/bare-api.json`:

```json
{"version":"2.0","routeKey":"GET /api/devices","rawPath":"/api/devices","headers":{},"requestContext":{"routeKey":"GET /api/devices","http":{"method":"GET","path":"/api/devices"}},"isBase64Encoded":false}
```

The user runs, or tells the controller to run:

```bash
aws lambda invoke --region us-east-1 --function-name scoreboard-api --cli-binary-format raw-in-base64-out --payload file://<scratchpad>/forged-api.json <scratchpad>/forged-api.out && cat <scratchpad>/forged-api.out
aws lambda invoke --region us-east-1 --function-name scoreboard-api --cli-binary-format raw-in-base64-out --payload file://<scratchpad>/bare-api.json <scratchpad>/bare-api.out && cat <scratchpad>/bare-api.out
```

Expected: both outputs are `"statusCode":401`. Then:

```bash
aws logs filter-log-events --region us-east-1 --log-group-name /aws/lambda/scoreboard-api --start-time <ms before invokes> --filter-pattern '"token rejected after authorizer accepted"' --query 'length(events)'
aws cloudwatch describe-alarms --region us-east-1 --alarm-names scoreboard-token-mismatch --query 'MetricAlarms[0].StateValue'
```

Expected:
- The log count is `1`: the forged event only.
- The alarm is `ALARM` within about five minutes.
- The email reaches the security topic's inbox; check Gmail.

Record everything in the ledger, for the verification record.

---

### Task 6: HockeyTrack apply A and record capture (controller and user; no subagent)

- [ ] **Step 1: Plan**

```bash
cd /home/jay/projects/hockeytrack
tag=$(aws lambda get-function --region us-east-1 --function-name hockeytrack-poller --query Code.ImageUri --output text | sed 's/.*://')
terraform -chdir=terraform plan -var="image_tag=$tag" -out=<scratchpad>/invoke-A.tfplan
terraform -chdir=terraform show -no-color <scratchpad>/invoke-A.tfplan | grep -E "will be|must be|Plan:"
```

Expected: `Plan: 0 to add, 3 to change, 0 to destroy.`, covering `aws_cloudtrail.account` (the new selector) and `aws_cloudwatch_event_rule.scoreboard_signin` and `.scoreboard_api` (pattern). Read the pattern diff: the only change is the added `eventCategory`.

- [ ] **Step 2: The user applies**

`! terraform -chdir=/home/jay/projects/hockeytrack/terraform apply <scratchpad>/invoke-A.tfplan`

Expected emails:
- audit-tampering for `PutEventSelectors`;
- the section 9 rewrite rule for the two `PutRule` calls.

Record which arrived.

- [ ] **Step 3: Generate the three kinds of invocation**

Note the time in ms, then:
- The user signs in to the admin site and loads the panel list. This produces Cognito's authgate invokes and API Gateway's api invokes.
- The user runs, or authorizes, `aws lambda invoke --region us-east-1 --function-name scoreboard-api --payload '{}' --cli-binary-format raw-in-base64-out <scratchpad>/empty.out`. Expected: `"statusCode":401`.

- [ ] **Step 4: Capture the records**

Wait up to 15 minutes for CloudTrail's CloudWatch Logs delivery, then:

```bash
aws logs filter-log-events --region us-east-1 --log-group-name /aws/cloudtrail/hockeytrack-account --start-time <ms> --filter-pattern '{ ($.eventSource = "lambda.amazonaws.com") && ($.eventCategory = "Data") }' --output json > <scratchpad>/invoke-records.json
python3 - <<'EOF'
import json
for e in json.load(open("<scratchpad>/invoke-records.json"))["events"]:
    r = json.loads(e["message"])
    u = r.get("userIdentity", {})
    print(r["eventTime"], r["eventName"], u.get("type"), u.get("invokedBy"), u.get("arn"),
          json.dumps(r.get("requestParameters")), json.dumps(r.get("resources")))
EOF
```

- [ ] **Step 5: Settle A1 and A2 in the ledger, as rulings**

- **A1 holds if** every authgate record from the sign-in has `userIdentity.invokedBy` = `cognito-idp.amazonaws.com`, every api record from the panel list has `apigateway.amazonaws.com`, and the direct invoke has no `invokedBy`. If a service call identifies itself any other way, **stop and return to the user**: section 12's shape depends on it.
- **A2:**
  - If `requestParameters.functionName` is present, record its form (bare name, unqualified ARN, or qualified ARN). Task 7's pattern lists all three forms, so any of them works unchanged.
  - If `functionName` is absent but `resources[].ARN` carries the function, rule that Task 7 replaces each `"requestParameters" = { "functionName" = X }` with `"resources" = { "ARN" = X }`, and carry that into Task 7's dispatch.
- **Silence check:** the `MatchedEvents` sum for `hockeytrack-sec-scoreboard-signin` and `hockeytrack-sec-scoreboard-api` from the apply to now must be zero, or be explained by the apply's own writes:

```bash
for r in hockeytrack-sec-scoreboard-signin hockeytrack-sec-scoreboard-api; do
  aws cloudwatch get-metric-statistics --region us-east-1 --namespace AWS/Events --metric-name MatchedEvents --dimensions Name=RuleName,Value=$r --start-time <ISO apply time> --end-time <ISO now> --period 300 --statistics Sum --query 'Datapoints[].Sum'
done
```

---

### Task 7: HockeyTrack section 12 and the recovery entry

**Repository:** `/home/jay/projects/hockeytrack`, branch `scoreboard-invoke-detection`.

**Files:**
- Modify: `terraform/security-alarms.tf` (the registry's `security_rules` and `security_alert_meaning`; a new section 12 after `aws_cloudwatch_event_rule.scoreboard_api`)
- Modify: `docs/threat-model.md` (§4 new paragraph after the admin API paragraph; §7 new entry after "A scoreboard admin API alert you cannot account for.")

**Interfaces:**
- **Consumes:**
  - `data.aws_lambda_function.scoreboard_admin_path` (Task 4).
  - The A2 ruling from Task 6's ledger. If the ruling says `resources.ARN`, make that substitution in all four branches.
  - The capture date and counts from Task 6, which go into the comment's "Measured" sentence. The controller supplies them in the dispatch.

- [ ] **Step 1: Add section 12**

After `resource "aws_cloudwatch_event_rule" "scoreboard_api" { ... }` in `terraform/security-alarms.tf`:

```hcl
# ---- 12. Direct invocation of the scoreboard's admin-path functions ----
#
# Sections 10 and 11 watch changes to the sign-in gate and the admin API. This
# watches calls. scoreboard-api and scoreboard-enroll exist to be invoked by
# API Gateway, and scoreboard-authgate by Cognito; each function's resource
# policy grants only that service. Anyone in this account whose own policy
# allows lambda:InvokeFunction can still invoke them directly with an event
# they wrote. The two API functions no longer believe such an event's claims
# (they verify the ID token themselves), but a genuine token replayed that way
# would be served, and authgate's events carry no token at all. So any
# invocation not made by the one service each function exists for pages.
#
# The trail logs these invocations as Lambda data events (cloudtrail.tf).
# Measured on <capture date from Task 6>: API Gateway's invocations carry
# userIdentity.invokedBy "apigateway.amazonaws.com", Cognito's
# "cognito-idp.amazonaws.com", and a direct invoke by an IAM user carries no
# invokedBy at all. anything-but does not match a missing field, so each
# function group has a second branch for exists false.
#
# Each function is listed as CloudTrail may name it: bare, as an unqualified
# ARN, and by prefix as an ARN qualified with a version or alias. The prefix
# ends in a colon, so scoreboard-api cannot match a future scoreboard-api-v2.
# No event names are listed, so Invoke, asynchronous invokes and
# InvokeWithResponseStream are covered alike. Management events never match:
# eventCategory is Data.
#
# What it does not see, the most important first:
#   - A genuine token used through the API. Nothing about that call is
#     unusual; token theft is a session problem. Tokens last an hour
#     (id_token_validity in the scoreboard's admin.tf).
#   - scoreboard-reducer and scoreboard-today. EventBridge invokes the reducer
#     on every game event, so logging it multiplies data-event volume, and a
#     forged invocation corrupts displayed game state without granting control
#     of a panel or a certificate. Scheduler invokes today through its own role,
#     and a direct invoke only republishes today's schedule.
#   - A new route or permission that makes API Gateway or Cognito itself the
#     caller: those are writes that sections 10 and 11 page on.
#   - Invocations while the trail is not logging, which the audit rule pages on
#     when logging stops or the selectors change.
#   - Rewriting this rule, which section 9 catches.
locals {
  scoreboard_invoke_arn = { for name, f in data.aws_lambda_function.scoreboard_admin_path : name => f.arn }

  scoreboard_invoke_api_path = [
    "scoreboard-api", local.scoreboard_invoke_arn["scoreboard-api"], { "prefix" = "${local.scoreboard_invoke_arn["scoreboard-api"]}:" },
    "scoreboard-enroll", local.scoreboard_invoke_arn["scoreboard-enroll"], { "prefix" = "${local.scoreboard_invoke_arn["scoreboard-enroll"]}:" },
  ]
  scoreboard_invoke_gate = [
    "scoreboard-authgate", local.scoreboard_invoke_arn["scoreboard-authgate"], { "prefix" = "${local.scoreboard_invoke_arn["scoreboard-authgate"]}:" },
  ]

  scoreboard_invoke_pattern = jsonencode({
    "detail-type" = ["AWS API Call via CloudTrail"]
    "detail" = {
      "eventSource"   = ["lambda.amazonaws.com"]
      "eventCategory" = ["Data"]
      "$or" = [
        { "requestParameters" = { "functionName" = local.scoreboard_invoke_api_path }, "userIdentity" = { "invokedBy" = [{ "anything-but" = ["apigateway.amazonaws.com"] }] } },
        { "requestParameters" = { "functionName" = local.scoreboard_invoke_api_path }, "userIdentity" = { "invokedBy" = [{ "exists" = false }] } },
        { "requestParameters" = { "functionName" = local.scoreboard_invoke_gate }, "userIdentity" = { "invokedBy" = [{ "anything-but" = ["cognito-idp.amazonaws.com"] }] } },
        { "requestParameters" = { "functionName" = local.scoreboard_invoke_gate }, "userIdentity" = { "invokedBy" = [{ "exists" = false }] } },
      ]
    }
  })
}

resource "aws_cloudwatch_event_rule" "scoreboard_invoke" {
  name          = "hockeytrack-sec-scoreboard-invoke"
  description   = "Any invocation of scoreboard-api or scoreboard-enroll not made by API Gateway, or of scoreboard-authgate not made by Cognito: someone calling them directly with an event they wrote"
  event_pattern = local.scoreboard_invoke_pattern

  lifecycle {
    precondition {
      condition     = length(local.scoreboard_invoke_pattern) <= 2048
      error_message = "The scoreboard invoke rule's event pattern is ${length(local.scoreboard_invoke_pattern)} characters. EventBridge rejects patterns over 2048, and only at apply."
    }
  }
}
```

Replace `<capture date from Task 6>` with the date the controller gives in the dispatch. If Task 6 measured different `invokedBy` values, the controller will have stopped already, so the values above stand.

- [ ] **Step 2: Register the rule**

In `local.security_rules`, add `scoreboard_invoke = aws_cloudwatch_event_rule.scoreboard_invoke`. In `local.security_alert_meaning`, add:

```hcl
    scoreboard_invoke = "If this was not you, assume someone with credentials in this account called a scoreboard admin function directly, skipping API Gateway or Cognito. Find the caller and access key in the CloudTrail record, check what the function did in its logs at that time, revoke the key, then check the admin API and sign-in gate against the scoreboard repository."
```

Keep `terraform fmt` alignment across the map.

- [ ] **Step 3: Add the §4 paragraph**

In `docs/threat-model.md`, after the paragraph that begins `**Changing the scoreboard's admin API pages someone.**`, add:

```markdown
**Calling the scoreboard's admin functions directly pages someone, and forged
claims are refused.** API Gateway's authorizer checks a token and passes its
claims to the function in the event, but a function cannot tell that event from
one written by hand and sent with `lambda:Invoke`. Anyone in the account allowed
that call could once have acted as any panel owner. Two things close it. The
API and enrollment functions verify the ID token themselves (signature against
the pool's published keys, issuer, exact audience, token use, expiry) and never
read identity from the authorizer's claims; an event whose authorizer block
says a token was accepted but whose token fails logs a line the scoreboard
alarms on. And the trail now logs invocations of those two functions and the
sign-in gate, and a rule pages on any not made by API Gateway or, for the gate,
Cognito. The rule is what sees a genuine token replayed through a direct invoke,
and anything sent to the gate, whose events carry no token. It does not watch
the reducer or the daily schedule function, whose forged invocations would
corrupt displayed game state but grant no control of a panel.
```

- [ ] **Step 4: Add the §7 recovery entry**

In `docs/threat-model.md` §7, directly after the whole "A scoreboard admin API alert you cannot account for." entry (after its last numbered step):

```markdown
**A scoreboard direct-invoke alert you cannot account for.** Assume someone
holds a credential in this account and has called a scoreboard admin function
with an event they wrote. In us-east-1:
1. Find the full record. The alert gives the time and the Actor ARN:
   `aws logs filter-log-events --log-group-name /aws/cloudtrail/hockeytrack-account --start-time <ms> --filter-pattern '{ ($.eventSource = "lambda.amazonaws.com") && ($.eventCategory = "Data") }'`.
   Note `userIdentity` (its `arn`, `accessKeyId`, and for a role
   `sessionContext.sessionIssuer`), `sourceIPAddress`, which function, and the
   exact `eventTime`.
2. Cut the credential off before investigating further. For an IAM user's key,
   `aws iam update-access-key --user-name <user> --access-key-id <id> --status Inactive`.
   For a role, revoke its active sessions (IAM console, the role, Revoke active
   sessions). Then follow the root sign-in procedure's credential steps for
   whoever owns it.
3. Read what the function did, from one minute before to five minutes after:
   `aws logs filter-log-events --log-group-name /aws/lambda/<function> --start-time <ms> --end-time <ms>`.
   - For `scoreboard-api` or `scoreboard-enroll`, a
     `token rejected after authorizer accepted` line means the function refused
     a forged event, and `scoreboard-token-mismatch` will have paged as well.
   - No such line, and no error, means the event may have carried a genuine
     token and been served. The logs do not record whose. Run the admin API
     entry's panel check (step 6) and certificate check (step 8), and sign
     every user out: `aws cognito-idp list-users --user-pool-id <pool id>`, then
     `aws cognito-idp admin-user-global-sign-out --user-pool-id <pool id> --username <username>`
     for each. That revokes refresh tokens; ID tokens already issued stay valid
     until they expire, at most an hour.
   - For `scoreboard-authgate`, a direct invoke gets its caller nothing: the
     gate's answer only matters when Cognito asks. Check its log for the
     decision, then run the sign-in entry anyway, because a credential that can
     invoke the gate can usually change it.
4. Run the admin API entry in full for `scoreboard-api` or `scoreboard-enroll`,
   and the sign-in entry for `scoreboard-authgate`.
```

- [ ] **Step 5: Validate**

```bash
cd /home/jay/projects/hockeytrack
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```

Expected: silent `fmt`, "Success!".

- [ ] **Step 6: Commit**

```bash
git add terraform/security-alarms.tf docs/threat-model.md
git commit -m "security: page on direct invocation of the scoreboard's admin functions

Section 12, hockeytrack-sec-scoreboard-invoke: any Lambda data event for
scoreboard-api or scoreboard-enroll not invoked by API Gateway, or for
scoreboard-authgate not invoked by Cognito. Threat model section 4 paragraph
and a section 7 recovery entry.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ckux9bzkCV2K2XAp8HkM2i"
```

---

### Task 8: HockeyTrack apply B, breaks and the verification record (controller and user; no subagent)

- [ ] **Step 1: Plan, and test the pattern before applying**

```bash
cd /home/jay/projects/hockeytrack
terraform -chdir=terraform plan -var="image_tag=$tag" -out=<scratchpad>/invoke-B.tfplan
terraform -chdir=terraform show -no-color <scratchpad>/invoke-B.tfplan | grep -E "will be|must be|Plan:"
```

Expected: `Plan: 2 to add, 2 to change, 0 to destroy.`
- **Added:** `aws_cloudwatch_event_rule.scoreboard_invoke` and `aws_cloudwatch_event_target.security["scoreboard_invoke"]`.
- **Changed:** the two policies that list every rule's ARN: the security topic policy (`security-alarms.tf` around line 109) and the DLQ queue policy (around line 361).

Rebuild the rendered `event_pattern` of `aws_cloudwatch_event_rule.scoreboard_invoke` from the plan output as JSON in `<scratchpad>/invoke-pattern.json`. Then run `aws events test-event-pattern` on every record in `<scratchpad>/invoke-records.json`, each wrapped as:

```json
{"version":"0","id":"test","detail-type":"AWS API Call via CloudTrail","source":"aws.lambda","account":"989232581535","time":"<eventTime>","region":"us-east-1","resources":[],"detail":<record>}
```

Expected: `true` only for the direct invoke; `false` for every Cognito-invoked authgate record and every API Gateway-invoked api record. Also test one API Gateway record with `invokedBy` edited to `events.amazonaws.com`: `true`.

- [ ] **Step 2: The user applies**

`! terraform -chdir=/home/jay/projects/hockeytrack/terraform apply <scratchpad>/invoke-B.tfplan`

Expected: the section 9 rewrite email for `PutRule` and `PutTargets`.

- [ ] **Step 3: Breaks**

Each break is a direct invoke, run by the user or on their explicit say-so. Write the events to the scratchpad:

- `break-api.json`: `{}`.
- `break-enroll.json`: `{"version":"2.0","routeKey":"GET /api/enroll","headers":{},"requestContext":{"routeKey":"GET /api/enroll"},"isBase64Encoded":false}`
- `break-authgate.json`: `{"version":"1","region":"us-east-1","userPoolId":"us-east-1_xJ6aWqZfR","userName":"Google_000000000000000000000","callerContext":{"awsSdkVersion":"aws-sdk-unknown-unknown","clientId":"1b27hh77mf6osi6agpmdlngeth"},"triggerSource":"PreSignUp_ExternalProvider","request":{"userAttributes":{"email":"break-test@example.com","email_verified":"true"}},"response":{"autoConfirmUser":false,"autoVerifyEmail":false,"autoVerifyPhone":false}}`

```bash
for f in api enroll authgate; do
  aws lambda invoke --region us-east-1 --function-name scoreboard-$f --cli-binary-format raw-in-base64-out --payload file://<scratchpad>/break-$f.json <scratchpad>/break-$f.out; cat <scratchpad>/break-$f.out; echo
done
```

Expected results:
- **api:** `401`.
- **enroll:** a non-2xx refusal.
- **authgate:** a function error whose log says `sign-in refused` / `not invited`.

Expected emails: within about 15 minutes, three with the section 12 sentence, each naming `Invoke` and the `funandgames` ARN. `scoreboard-signin-refused` does not fire from one refusal, unless there were two others in the past hour; record it if so.

- [ ] **Step 4: Negatives and delivery health**

- The user signs in and loads the panel list. Expected: no section 10, 11 or 12 email.
- **Metrics,** from before the breaks to 20 minutes after the negatives:

```bash
for m in MatchedEvents Invocations FailedInvocations; do
  aws cloudwatch get-metric-statistics --region us-east-1 --namespace AWS/Events --metric-name $m --dimensions Name=RuleName,Value=hockeytrack-sec-scoreboard-invoke --start-time <ISO> --end-time <ISO> --period 300 --statistics Sum --query 'Datapoints[].Sum'
done
aws sqs get-queue-attributes --region us-east-1 --queue-url "$(aws sqs get-queue-url --region us-east-1 --queue-name hockeytrack-security-alerts-dlq --query QueueUrl --output text)" --attribute-names ApproximateNumberOfMessages
```

  Expected: `MatchedEvents` and `Invocations` sum to 3, `FailedInvocations` to 0, and the DLQ holds 0.
- **If `MatchedEvents` stays at 0** while Step 1's `test-event-pattern` matched the direct invoke, A3 has failed: EventBridge is not receiving Lambda data events. **Stop and return to the user**, with the spec's fallback: a metric filter on the trail's log group.

- [ ] **Step 5: Recovery entry, read-only**

Run steps 1 and 3 of the new §7 entry against the api break's record: find the record, then read the function's log window. Confirm every command works and returns what the entry says. Do not run step 2's write, the global sign-out, or any other mutating command.

- [ ] **Step 6: Verification record and drift**

Append `## 8. Verification record (<date>)` to `docs/superpowers/specs/2026-09-15-direct-invoke-design.md` in the scoreboard repository. It records:
- Task 5's results;
- A1, A2 and A3 as measured;
- the `test-event-pattern` table;
- the breaks and emails;
- the negatives;
- the metrics and DLQ depth;
- the recovery run.

Commit it on `direct-invoke`. Then:

```bash
cd /home/jay/projects/hockeytrack-scoreboard && make build && terraform -chdir=terraform plan -detailed-exitcode
cd /home/jay/projects/hockeytrack && terraform -chdir=terraform plan -var="image_tag=$tag" -detailed-exitcode
```

Expected: both exit `0`.

- [ ] **Step 7: Finish**

Use superpowers:finishing-a-development-branch for both branches.
