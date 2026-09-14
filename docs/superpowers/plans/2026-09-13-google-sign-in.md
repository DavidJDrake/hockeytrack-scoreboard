# Sign in with Google Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the admin site's email-and-password sign-in with Sign in with Google, while keeping the site invite-only.

**Architecture:** A new Go Lambda, `scoreboard-authgate`, is attached to the Cognito pool as both its pre sign-up trigger and its pre token generation (V1_0) trigger. It admits a Google account only if its address is verified by Google and on an allowlist held in one SSM parameter. It checks again at every token issuance, so removing someone ends their access. Terraform adds the Google identity provider, the parameter, the function and an alarm, and it makes Google the client's only provider with no password flows. The static site sends Sign in straight to Google and gains a privacy page.

**Tech Stack:** Go 1.27 (aws-lambda-go v1.55.0, aws-sdk-go-v2 SSM), Terraform with AWS provider 5.100, plain ES modules tested with `node --test`.

**Spec:** `docs/superpowers/specs/2026-09-13-google-sign-in-design.md`

## Global Constraints

- US spelling everywhere: enroll, enrollment, authorize, behavior.
- Both repositories are PUBLIC. Never commit `terraform/terraform.tfvars`, `device/config/`, or any certificate, private key, credential or real email address. Example addresses use `example.com`.
- Subagents never run `terraform apply`, `terraform plan`, any mutating AWS call, `make deploy`, `make site`, `make provision`, `tools/provision.sh`, or `git push`. Only the controller creates `terraform/terraform.tfvars`.
- Nobody prints the Google client secret. Terraform reads it to plan, and that is the only reader. That rules out `aws secretsmanager get-secret-value`, `terraform show -json`, a plan's JSON form, and `terraform console` on `local.google_oauth`, because each prints sensitive values in the clear.
- Every AWS CLI command passes `--region us-east-1`. This machine's CLI defaults to us-east-2, where none of this stack exists.
- The gate fails closed. Any doubt — an unexpected trigger, an unreadable invite list, a malformed event — refuses.
- Refusals log the trigger source, a reason and the email's domain. They never log the full address or its local part.
- Every refusal returns the identical error, `errRefused`. Cognito passes its message to the browser, so it must not reveal which check failed.
- Addresses compare with `enroll.NormalizeOwner`: trimmed and lowercased, nothing else. Invites and ownership checks must agree on what counts as the same address.
- SSM parameter name `/scoreboard/allowed-emails`; Secrets Manager secret `scoreboard/google-oauth-client` (JSON keys `client_id`, `client_secret`); function `scoreboard-authgate`; alarm `scoreboard-signin-refused`; environment variable `ALLOWLIST_PARAMETER`.
- Match the surrounding code: gofmt, `terraform fmt`, and comments that explain why rather than what.

---

## File Structure

| File | Responsibility |
|---|---|
| `cloud/cmd/authgate/allowlist.go` (new) | The invite list: fetch, 60-second cache, fail-closed on read errors |
| `cloud/cmd/authgate/handler.go` (new) | Dispatch on `triggerSource`; the pre sign-up and token generation rules; refusal logging |
| `cloud/cmd/authgate/main.go` (new) | Wiring: SSM client, environment, `lambda.Start` |
| `cloud/cmd/authgate/*_test.go` (new) | Tests for both |
| `cloud/go.mod`, `cloud/go.sum` | Add `service/ssm` |
| `Makefile` | Build `build/authgate.zip` |
| `terraform/signin.tf` (new) | Invite variable, SSM parameter, Google IdP, authgate function and IAM, metric filter and alarm |
| `terraform/admin.tf` | Pool `lambda_config`; client providers and auth flows; comments |
| `site/tests/signin-config.test.js` (new) | Tripwires on the Terraform sign-in settings |
| `site/assets/auth.js`, `site/tests/auth.test.js` | `identity_provider=Google` |
| `site/assets/app.js` | The message shown when sign-in comes back with an error |
| `site/index.html` | Button copy; footer link to the privacy page |
| `site/privacy/index.html` (new) | The privacy policy |
| `site/tests/pages.test.js` (new) | Page-level checks: button copy, privacy link, privacy page runs no script |
| `docs/admin-api.md`, `docs/hardware-checks.md`, two earlier specs | Describe the new sign-in |

---

### Task 1: The authgate function

**Files:**
- Create: `cloud/cmd/authgate/allowlist.go`
- Create: `cloud/cmd/authgate/allowlist_test.go`
- Create: `cloud/cmd/authgate/handler.go`
- Create: `cloud/cmd/authgate/handler_test.go`
- Create: `cloud/cmd/authgate/main.go`
- Modify: `cloud/go.mod`, `cloud/go.sum`
- Modify: `Makefile` (the `build` target)

**Interfaces:**
- Consumes: `enroll.NormalizeOwner(string) string` from `hockeytrack-scoreboard/internal/enroll`; `events.CognitoEventUserPoolsHeader`, `events.CognitoEventUserPoolsPreSignup`, `events.CognitoEventUserPoolsPreTokenGen` from aws-lambda-go v1.55.0.
- Produces: a Lambda binary built to `build/authgate/bootstrap` and zipped to `build/authgate.zip`, which reads its parameter name from the `ALLOWLIST_PARAMETER` environment variable. Task 2's Terraform consumes both.

Run Go commands with Go on PATH: `export PATH=$PATH:$HOME/.local/share/go/bin`.

- [ ] **Step 1: Write the failing allowlist tests**

Create `cloud/cmd/authgate/allowlist_test.go`:

```go
package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

// clock is a time source a test can move, in either direction.
type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

// source stands in for the SSM parameter: a test can edit it, break it, and
// count how often it is read.
type source struct {
	value string
	err   error
	reads int
}

func (s *source) fetch(context.Context) (string, error) {
	s.reads++
	return s.value, s.err
}

func newAllowlist(src *source, c *clock) *Allowlist {
	return &Allowlist{Fetch: src.fetch, TTL: time.Minute, Now: c.now}
}

func newClock() *clock { return &clock{t: time.Unix(1_800_000_000, 0)} }

func mustContain(t *testing.T, a *Allowlist, email string, want bool) {
	t.Helper()
	got, err := a.Contains(context.Background(), email)
	if err != nil {
		t.Fatalf("Contains(%q): %v", email, err)
	}
	if got != want {
		t.Fatalf("Contains(%q) = %v, want %v", email, got, want)
	}
}

func TestAllowlistReadsAtMostOncePerTTL(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	c.advance(59 * time.Second)
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 1 {
		t.Fatalf("reads within the TTL = %d, want 1", src.reads)
	}
	c.advance(time.Second)
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 2 {
		t.Fatalf("reads once the TTL has passed = %d, want 2", src.reads)
	}
}

func TestAllowlistRemovalTakesEffectOnceTheTTLPasses(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	src.value = "someone.else@example.com"
	c.advance(59 * time.Second)
	// The documented cost of the cache: up to a minute of grace.
	mustContain(t, a, "owner@example.com", true)
	c.advance(time.Second)
	mustContain(t, a, "owner@example.com", false)
}

func TestAllowlistFailsClosedRatherThanUsingAStaleList(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	c.advance(time.Minute)
	src.err = errors.New("ssm unavailable")
	got, err := a.Contains(context.Background(), "owner@example.com")
	if err == nil || got {
		t.Fatalf("Contains with SSM down = %v, %v; want false and an error", got, err)
	}

	// A failed read is not remembered as an answer: the next call tries again.
	src.err = nil
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 3 {
		t.Fatalf("reads = %d, want 3", src.reads)
	}
}

func TestAllowlistNormalizesCaseAndWhitespaceOnly(t *testing.T) {
	src := &source{value: " Owner@Example.com ,, second@example.com"}
	a := newAllowlist(src, newClock())

	mustContain(t, a, "OWNER@example.COM ", true)
	mustContain(t, a, "second@example.com", true)
	// Plus addressing and dots are meaningful to Cognito, so they are
	// different people here too.
	mustContain(t, a, "owner+x@example.com", false)
	mustContain(t, a, "o.wner@example.com", false)
	// The empty entries in the list must not admit an empty address.
	mustContain(t, a, "", false)
	mustContain(t, a, "   ", false)
}

func TestAllowlistRereadsWhenTheClockGoesBackwards(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	c.advance(-time.Second)
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 2 {
		t.Fatalf("reads after the clock moved backwards = %d, want 2", src.reads)
	}
}
```

- [ ] **Step 2: Run the allowlist tests and confirm they fail**

Run: `cd cloud && go test ./cmd/authgate/`
Expected: FAIL to build, with `undefined: Allowlist`.

- [ ] **Step 3: Implement the allowlist**

Create `cloud/cmd/authgate/allowlist.go`:

```go
package main

import (
	"context"
	"strings"
	"sync"
	"time"

	"hockeytrack-scoreboard/internal/enroll"
)

// Allowlist is the set of invited addresses: one comma-separated SSM
// parameter, read at most once per TTL by each warm Lambda instance, so a
// burst of sign-ins is not a burst of reads. The TTL is also the longest a
// removal waits to take effect.
type Allowlist struct {
	Fetch func(ctx context.Context) (string, error)
	TTL   time.Duration
	Now   func() time.Time

	mu      sync.Mutex
	members map[string]bool
	fetched time.Time
}

// Contains reports whether email is invited. Addresses compare the way the
// enroll flow compares an owner hint -- case and surrounding whitespace only --
// so an invitation and an ownership check never disagree about whether two
// spellings are the same person.
func (a *Allowlist) Contains(ctx context.Context, email string) (bool, error) {
	email = enroll.NormalizeOwner(email)

	a.mu.Lock()
	defer a.mu.Unlock()

	now := a.Now()
	if a.members == nil || now.Sub(a.fetched) >= a.TTL || now.Before(a.fetched) {
		raw, err := a.Fetch(ctx)
		if err != nil {
			// No falling back to the list already held. It is stale by
			// definition here, and serving it would keep admitting someone
			// just removed for as long as SSM stayed unreachable.
			a.members = nil
			return false, err
		}
		a.members = parseAllowlist(raw)
		a.fetched = now
	}
	return email != "" && a.members[email], nil
}

func parseAllowlist(raw string) map[string]bool {
	members := map[string]bool{}
	for _, entry := range strings.Split(raw, ",") {
		if e := enroll.NormalizeOwner(entry); e != "" {
			members[e] = true
		}
	}
	return members
}
```

- [ ] **Step 4: Run the allowlist tests and confirm they pass**

Run: `cd cloud && go test ./cmd/authgate/`
Expected: PASS. The package has no `main` function until Step 9; `go test` builds it without one.

- [ ] **Step 5: Write the failing handler tests**

Create `cloud/cmd/authgate/handler_test.go`:

```go
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
)

const invited = "owner@example.com"

// gate is a Handler wired to a fake parameter, a movable clock and a log
// buffer the test can read.
type gate struct {
	*Handler
	src   *source
	clock *clock
	logs  *bytes.Buffer
}

func newGate(list string) *gate {
	src := &source{value: list}
	c := newClock()
	logs := &bytes.Buffer{}
	return &gate{
		Handler: &Handler{
			Allowlist: newAllowlist(src, c),
			Log:       slog.New(slog.NewTextHandler(logs, nil)),
		},
		src:   src,
		clock: c,
		logs:  logs,
	}
}

// event builds a trigger event in the shape Cognito sends one.
func event(t *testing.T, triggerSource string, attrs map[string]string) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"version":       "1",
		"region":        "us-east-1",
		"userPoolId":    "us-east-1_example",
		"userName":      "Google_117000000000000000000",
		"callerContext": map[string]string{"awsSdkVersion": "aws-sdk-unknown-unknown", "clientId": "client123"},
		"triggerSource": triggerSource,
		"request":       map[string]any{"userAttributes": attrs},
		"response":      map[string]any{},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func verified(email string) map[string]string {
	return map[string]string{"email": email, "email_verified": "true"}
}

func mustRefuse(t *testing.T, out json.RawMessage, err error) {
	t.Helper()
	// Identity, not errors.Is: a wrapped error would carry extra text to the
	// browser, and the point is that every refusal reads the same.
	if err != errRefused {
		t.Fatalf("err = %v, want errRefused", err)
	}
	if out != nil {
		t.Fatalf("a refusal returned an event: %s", out)
	}
}

func TestPreSignUpAdmitsAnInvitedVerifiedGoogleAccount(t *testing.T) {
	g := newGate(invited)
	out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified(invited)))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	var got events.CognitoEventUserPoolsPreSignup
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Response.AutoConfirmUser || !got.Response.AutoVerifyEmail {
		t.Fatalf("response = %+v, want the account confirmed and its email verified", got.Response)
	}
	if got.TriggerSource != "PreSignUp_ExternalProvider" || got.UserName != "Google_117000000000000000000" {
		t.Fatalf("header not returned intact: %+v", got.CognitoEventUserPoolsHeader)
	}
}

func TestPreSignUpRefusesEveryOtherSourceEvenForAnInvitedAddress(t *testing.T) {
	// ExternalProviderX proves the check is an exact match, not a prefix.
	for _, src := range []string{"PreSignUp_SignUp", "PreSignUp_AdminCreateUser", "PreSignUp_ExternalProviderX"} {
		t.Run(src, func(t *testing.T) {
			g := newGate(invited)
			out, err := g.Handle(context.Background(), event(t, src, verified(invited)))
			mustRefuse(t, out, err)
		})
	}
}

func TestPreSignUpRefusesAnAddressGoogleHasNotVerified(t *testing.T) {
	cases := map[string]map[string]string{
		"false":   {"email": invited, "email_verified": "false"},
		"empty":   {"email": invited, "email_verified": ""},
		"missing": {"email": invited},
	}
	for name, attrs := range cases {
		t.Run(name, func(t *testing.T) {
			g := newGate(invited)
			out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", attrs))
			mustRefuse(t, out, err)
		})
	}
}

func TestPreSignUpRefusesAnUninvitedAddress(t *testing.T) {
	g := newGate(invited)
	out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified("stranger@example.com")))
	mustRefuse(t, out, err)
}

func TestPreSignUpIgnoresCaseAndSurroundingWhitespace(t *testing.T) {
	g := newGate(" Owner@Example.com ")
	_, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified("OWNER@example.COM ")))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
}

func TestAnUnreadableInviteListRefusesAtBothTriggers(t *testing.T) {
	for _, src := range []string{"PreSignUp_ExternalProvider", "TokenGeneration_HostedAuth"} {
		t.Run(src, func(t *testing.T) {
			g := newGate(invited)
			g.src.err = errors.New("ssm unavailable")
			out, err := g.Handle(context.Background(), event(t, src, verified(invited)))
			mustRefuse(t, out, err)
			if !strings.Contains(g.logs.String(), `reason="invite list unavailable"`) {
				t.Fatalf("log does not name the cause:\n%s", g.logs)
			}
		})
	}
}

func TestTokenGenerationPassesAnInvitedAccountThroughUnchanged(t *testing.T) {
	for _, src := range []string{"TokenGeneration_HostedAuth", "TokenGeneration_RefreshTokens", "TokenGeneration_Authentication"} {
		t.Run(src, func(t *testing.T) {
			g := newGate(invited)
			in := event(t, src, verified(invited))
			out, err := g.Handle(context.Background(), in)
			if err != nil {
				t.Fatalf("refused: %v", err)
			}
			if !bytes.Equal(out, in) {
				t.Fatalf("event changed:\n in: %s\nout: %s", in, out)
			}
		})
	}
}

func TestTokenGenerationRefusesAnAddressTakenOffTheList(t *testing.T) {
	g := newGate(invited)
	if _, err := g.Handle(context.Background(), event(t, "TokenGeneration_HostedAuth", verified(invited))); err != nil {
		t.Fatalf("refused while still invited: %v", err)
	}
	g.src.value = "someone.else@example.com"
	g.clock.advance(time.Minute)
	out, err := g.Handle(context.Background(), event(t, "TokenGeneration_RefreshTokens", verified(invited)))
	mustRefuse(t, out, err)
}

func TestUnexpectedEventsAreRefused(t *testing.T) {
	cases := map[string]json.RawMessage{
		"another trigger":   event(t, "CustomMessage_SignUp", verified(invited)),
		"no trigger source": event(t, "", verified(invited)),
		"not JSON":          json.RawMessage(`{`),
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			g := newGate(invited)
			out, err := g.Handle(context.Background(), in)
			mustRefuse(t, out, err)
		})
	}
}

func TestRefusalsLogTheDomainButNotWhoItWas(t *testing.T) {
	g := newGate(invited)
	out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified("Stranger.Name@Example.com")))
	mustRefuse(t, out, err)

	logged := g.logs.String()
	for _, want := range []string{"sign-in refused", "trigger=PreSignUp_ExternalProvider", `reason="not invited"`, "domain=example.com"} {
		if !strings.Contains(logged, want) {
			t.Errorf("log lacks %q:\n%s", want, logged)
		}
	}
	if strings.Contains(strings.ToLower(logged), "stranger.name") {
		t.Errorf("log contains the address's local part:\n%s", logged)
	}
}
```

- [ ] **Step 6: Run the handler tests and confirm they fail**

Run: `cd cloud && go test ./cmd/authgate/`
Expected: FAIL to build, with `undefined: Handler` and `undefined: errRefused`.

- [ ] **Step 7: Implement the handler**

Create `cloud/cmd/authgate/handler.go`:

```go
// Command authgate decides who may sign in to the admin site.
//
// Cognito calls it at both ends of an account's life. As the pre sign-up
// trigger, it runs when a Google account signs in for the first time and
// Cognito is about to create a profile for it. As the pre token generation
// trigger, it runs every time tokens are issued. The pool's own invite-only
// setting governs the SignUp operation and nothing else, so without this
// function anyone with a Google account could make an account here. The
// design is docs/superpowers/specs/2026-09-13-google-sign-in-design.md.
//
// Every doubt resolves to no. Returning an error is how a trigger makes
// Cognito refuse, so a broken dependency refuses too.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/enroll"
)

// errRefused is the one answer every refusal gives. Cognito passes the
// message through to the browser, so it must not say which check failed:
// "not verified" against "not invited" would tell a stranger whether an
// address is on the list.
var errRefused = errors.New("this account is not invited")

type Handler struct {
	Allowlist *Allowlist
	Log       *slog.Logger
}

func (h *Handler) Handle(ctx context.Context, event json.RawMessage) (json.RawMessage, error) {
	var header events.CognitoEventUserPoolsHeader
	if err := json.Unmarshal(event, &header); err != nil {
		return h.refuse("", "malformed event", "")
	}
	switch {
	case strings.HasPrefix(header.TriggerSource, "PreSignUp_"):
		return h.preSignUp(ctx, event)
	case strings.HasPrefix(header.TriggerSource, "TokenGeneration_"):
		return h.tokenGeneration(ctx, event)
	}
	return h.refuse(header.TriggerSource, "unexpected trigger", "")
}

// preSignUp admits a new account only for a Google sign-in, with an address
// Google has verified, that is on the invite list.
func (h *Handler) preSignUp(ctx context.Context, event json.RawMessage) (json.RawMessage, error) {
	var ev events.CognitoEventUserPoolsPreSignup
	if err := json.Unmarshal(event, &ev); err != nil {
		return h.refuse("", "malformed event", "")
	}
	source := ev.TriggerSource
	email := ev.Request.UserAttributes["email"]

	// PreSignUp_SignUp and PreSignUp_AdminCreateUser are refused for every
	// address, invited or not. Otherwise anyone who knew an invited address
	// could make a password account for it, one Google never vouched for.
	if source != "PreSignUp_ExternalProvider" {
		return h.refuse(source, "not a Google sign-in", email)
	}
	// Panel ownership is decided from the verified email (ownerMatches in
	// cmd/enroll), so this site must not vouch for an address its identity
	// provider has not.
	if ev.Request.UserAttributes["email_verified"] != "true" {
		return h.refuse(source, "email not verified by Google", email)
	}
	if out, err := h.requireInvited(ctx, source, email); err != nil {
		return out, err
	}

	ev.Response.AutoConfirmUser = true
	ev.Response.AutoVerifyEmail = true
	return json.Marshal(ev)
}

// tokenGeneration refuses tokens to an account whose address has since been
// taken off the invite list. Checking only at sign-up would leave a removed
// person's account working indefinitely. It changes no claim: the event goes
// back exactly as it came.
func (h *Handler) tokenGeneration(ctx context.Context, event json.RawMessage) (json.RawMessage, error) {
	var ev events.CognitoEventUserPoolsPreTokenGen
	if err := json.Unmarshal(event, &ev); err != nil {
		return h.refuse("", "malformed event", "")
	}
	if out, err := h.requireInvited(ctx, ev.TriggerSource, ev.Request.UserAttributes["email"]); err != nil {
		return out, err
	}
	return event, nil
}

// requireInvited returns a nil error only when email is on the invite list.
func (h *Handler) requireInvited(ctx context.Context, source, email string) (json.RawMessage, error) {
	invited, err := h.Allowlist.Contains(ctx, email)
	if err != nil {
		return h.refuse(source, "invite list unavailable", email, "err", err)
	}
	if !invited {
		return h.refuse(source, "not invited", email)
	}
	return nil, nil
}

// refuse logs one line -- the metric filter in terraform/signin.tf counts
// exactly this message -- and returns the refusal.
func (h *Handler) refuse(source, reason, email string, attrs ...any) (json.RawMessage, error) {
	args := append([]any{"trigger", source, "reason", reason, "domain", domainOf(email)}, attrs...)
	h.Log.Warn("sign-in refused", args...)
	return nil, errRefused
}

// domainOf keeps the part of an address that says roughly where a refusal
// came from -- gmail.com, or a company -- and drops the part that says who.
// A stranger's address is personal data this project has no reason to keep.
func domainOf(email string) string {
	at := strings.LastIndex(email, "@")
	if at < 0 {
		return ""
	}
	return enroll.NormalizeOwner(email[at+1:])
}
```

- [ ] **Step 8: Run the handler tests and confirm they pass**

Run: `cd cloud && go test ./cmd/authgate/ -v 2>&1 | tail -30`
Expected: every test PASS.

- [ ] **Step 9: Add the SSM dependency and the entry point**

Run: `cd cloud && go get github.com/aws/aws-sdk-go-v2/service/ssm@v1.78.0`

Create `cloud/cmd/authgate/main.go`:

```go
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	name := os.Getenv("ALLOWLIST_PARAMETER")
	if name == "" {
		slog.Error("ALLOWLIST_PARAMETER is required")
		os.Exit(1)
	}
	client := ssm.NewFromConfig(cfg)
	h := &Handler{
		Allowlist: &Allowlist{
			Fetch: func(ctx context.Context) (string, error) {
				out, err := client.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(name)})
				if err != nil {
					return "", err
				}
				if out.Parameter == nil || out.Parameter.Value == nil {
					return "", errors.New("invite list parameter has no value")
				}
				return *out.Parameter.Value, nil
			},
			TTL: time.Minute,
			Now: time.Now,
		},
		Log: slog.Default(),
	}
	lambda.Start(h.Handle)
}
```

Run: `cd cloud && go mod tidy && git diff --stat go.mod go.sum`
Expected: `service/ssm` is added to `require`. Other `aws-sdk-go-v2` modules may move up a patch version; that is expected. Mention any such bump in your report.

- [ ] **Step 10: Build it into the Makefile**

In `Makefile`, replace:

```make
	mkdir -p build/reducer build/today build/api build/enroll
```

with:

```make
	mkdir -p build/reducer build/today build/api build/enroll build/authgate
```

and after the line `	cd build/enroll && python3 -m zipfile -c ../enroll.zip bootstrap` add:

```make
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/authgate/bootstrap ./cmd/authgate
	cd build/authgate && python3 -m zipfile -c ../authgate.zip bootstrap
```

- [ ] **Step 11: Run the whole Go gate**

Run: `make fmt test-go vuln-go build && ls -l build/authgate.zip`
Expected: gofmt prints nothing, vet and every test pass, govulncheck reports no vulnerabilities, and `build/authgate.zip` exists.

- [ ] **Step 12: Commit**

```bash
git add cloud/cmd/authgate cloud/go.mod cloud/go.sum Makefile
git commit -m "authgate: admit only invited, Google-verified addresses, at sign-up and at every token

The Cognito pool's invite-only setting closes SignUp and nothing else, so
a Google account's first sign-in would create an account for anyone. This
function is the gate. As the pre sign-up trigger it admits only a Google
sign-in whose address Google has verified and the invite list names. As
the pre token generation trigger it checks the list again, so taking
someone off it ends access they already have.

It fails closed: an unreadable list, an unexpected trigger or a malformed
event all refuse, and every refusal returns the same message, because
Cognito shows that message to the browser. Refusals log the email's
domain, never the address."
```

- [ ] **Step 13: Mutation checks (no commit)**

These prove the three most important checks are tested, not just present.

1. In `handler.go`, change `if source != "PreSignUp_ExternalProvider" {` to `if false && source != "PreSignUp_ExternalProvider" {`. Run `cd cloud && go test ./cmd/authgate/`. Expected: `TestPreSignUpRefusesEveryOtherSourceEvenForAnInvitedAddress` FAILS. Then run `git checkout -- cloud/cmd/authgate/handler.go`.
2. Change `if ev.Request.UserAttributes["email_verified"] != "true" {` to `if false && ev.Request.UserAttributes["email_verified"] != "true" {`. Run the tests. Expected: `TestPreSignUpRefusesAnAddressGoogleHasNotVerified` FAILS. Then run `git checkout -- cloud/cmd/authgate/handler.go`.
3. In `allowlist.go`, change `return false, err` in the fetch error branch to `return a.members[email], nil`. Run the tests. Expected: `TestAllowlistFailsClosedRatherThanUsingAStaleList` and `TestAnUnreadableInviteListRefusesAtBothTriggers` FAIL. Then run `git checkout -- cloud/cmd/authgate/allowlist.go`.

Report all three outcomes. Run `git status --short` last; it must show a clean tree.

---

### Task 2: Terraform: the provider, the gate, the invite list, the alarm

**Files:**
- Create: `terraform/signin.tf`
- Modify: `terraform/admin.tf` (pool: comment and `lambda_config`; client: comment, `supported_identity_providers`, `explicit_auth_flows`)
- Create: `site/tests/signin-config.test.js`

**Interfaces:**
- Consumes: from Task 1, `build/authgate/bootstrap` and the environment variable `ALLOWLIST_PARAMETER`. Existing resources: `aws_cognito_user_pool.admin`, `aws_cognito_user_pool_client.site`, `data.aws_iam_policy_document.lambda_trust`, `local.logs`, `data.aws_sns_topic.security_alerts`, `var.site_domain`.
- Produces: `aws_lambda_function.authgate`, `aws_ssm_parameter.allowed_emails`, `aws_cognito_identity_provider.google`, and the alarm `scoreboard-signin-refused`. It also adds a required variable, `invited_emails`, which the controller supplies in the gitignored `terraform/terraform.tfvars` before planning (Task 5).

Terraform is a snap and needs a writable runtime directory. Run first: `export XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime"; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"`.

- [ ] **Step 1: Write the failing tripwire tests**

Create `site/tests/signin-config.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Sign-in lives in Terraform, but a regression there is a regression in who
// can reach this site, so -- like csp.test.js -- its tripwires run with the
// site's tests. They read the configuration as text. They catch somebody
// quietly re-enabling a password path; they do not prove what AWS is running.
const read = (name) => readFileSync(new URL(`../../terraform/${name}`, import.meta.url), "utf8");
const admin = read("admin.tf");
let signin = "";
try {
  signin = read("signin.tf");
} catch {
  // Reported by the tests below, one failure each, rather than as a crash.
}

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}

// Comments are dropped, so a sentence explaining a setting can neither
// satisfy a test nor fail one.
const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");

test("the site's client signs in through Google and nothing else", () => {
  const client = code(block(admin, 'resource "aws_cognito_user_pool_client" "site" {'));
  const m = client.match(/supported_identity_providers\s*=\s*\[([^\]]*)\]/);
  assert.ok(m, "supported_identity_providers not found");
  assert.equal(m[1].trim(), "aws_cognito_identity_provider.google.provider_name");
});

test("the client allows no password flow, SRP included", () => {
  const client = code(block(admin, 'resource "aws_cognito_user_pool_client" "site" {'));
  const m = client.match(/explicit_auth_flows\s*=\s*\[([^\]]*)\]/);
  assert.ok(m, "explicit_auth_flows not found -- leaving it out lets Cognito default to password flows");
  assert.deepEqual(m[1].split(",").map((s) => s.trim()).filter(Boolean), ['"ALLOW_REFRESH_TOKEN_AUTH"']);
});

test("the pool runs the gate at sign-up and at every token issuance", () => {
  const pool = code(block(admin, 'resource "aws_cognito_user_pool" "admin" {'));
  assert.match(pool, /pre_sign_up\s*=\s*aws_lambda_function\.authgate\.arn/);
  const cfg = pool.match(/pre_token_generation_config\s*\{([^}]*)\}/);
  assert.ok(cfg, "pre_token_generation_config not found");
  assert.match(cfg[1], /lambda_arn\s*=\s*aws_lambda_function\.authgate\.arn/);
  assert.match(cfg[1], /lambda_version\s*=\s*"V1_0"/);
});

test("Google is asked for the verified flag the gate depends on", () => {
  const idp = code(block(signin, 'resource "aws_cognito_identity_provider" "google" {'));
  assert.match(idp, /email_verified\s*=\s*"email_verified"/);
  assert.match(idp, /authorize_scopes\s*=\s*"openid email"/);
});

test("Terraform can never overwrite the invite list", () => {
  const param = code(block(signin, 'resource "aws_ssm_parameter" "allowed_emails" {'));
  assert.match(param, /ignore_changes\s*=\s*\[\s*value\s*\]/);
});

test("no invited address is committed as a default", () => {
  const variable = code(block(signin, 'variable "invited_emails" {'));
  assert.doesNotMatch(variable, /default\s*=/);
});
```

- [ ] **Step 2: Run the tripwires and confirm they fail**

Run: `cd site && node --test tests/signin-config.test.js`
Expected: FAIL on all six. The first two find `["COGNITO"]` and the SRP flow; the rest cannot find `signin.tf` or `authgate`.

- [ ] **Step 3: Create `terraform/signin.tf`**

```hcl
# Sign in with Google, invite-only.
# Spec: docs/superpowers/specs/2026-09-13-google-sign-in-design.md
#
# The pool's allow_admin_create_user_only closes SignUp and nothing else: a
# Google account's first sign-in creates a profile regardless. So the invite
# list is enforced here, by one function the pool calls at both ends of an
# account's life -- when it is about to be created, and whenever tokens are
# issued -- which is what lets taking someone off the list end access they
# already have.

variable "invited_emails" {
  type        = list(string)
  sensitive   = true
  nullable    = false
  description = "Addresses the invite list starts with. Read ONCE, when /scoreboard/allowed-emails is first created; after that the list is edited with aws ssm put-parameter and this variable changes nothing. Set it in the gitignored terraform.tfvars, never in this public repository."

  validation {
    condition = length(var.invited_emails) > 0 && alltrue([
      for e in var.invited_emails : can(regex("^[^@,[:space:]]+@[^@,[:space:]]+$", e))
    ])
    error_message = "invited_emails needs at least one address, with no commas or spaces in any of them."
  }
}

# The invite list, as one comma-separated String -- the shape the ebook
# library's gate already runs on. Reading it takes ssm:GetParameter on this
# parameter, which the authgate role below holds and nothing else in this
# stack does.
resource "aws_ssm_parameter" "allowed_emails" {
  name        = "/scoreboard/allowed-emails"
  description = "Addresses allowed to sign in to ${var.site_domain}, comma-separated. Edit with aws ssm put-parameter --overwrite; Terraform never changes the value."
  type        = "String"
  value       = join(",", var.invited_emails)

  # Terraform seeds this once and then keeps its hands off, so no apply can
  # quietly undo an invitation or reinstate someone who was removed.
  lifecycle {
    ignore_changes = [value]
  }
}

# The Google OAuth client's ID and secret, created by hand so the secret is
# typed into nothing that gets committed. It does still land in Terraform
# state, because the identity provider below stores it; the state bucket is
# encrypted and private, and the spec (section 4.3) names that trade. Never
# inspect this with `terraform show -json` or a plan's JSON form, which print
# sensitive values in the clear.
data "aws_secretsmanager_secret_version" "google_oauth" {
  secret_id = "scoreboard/google-oauth-client"
}

locals {
  google_oauth = jsondecode(data.aws_secretsmanager_secret_version.google_oauth.secret_string)
}

resource "aws_cognito_identity_provider" "google" {
  user_pool_id  = aws_cognito_user_pool.admin.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id        = local.google_oauth.client_id
    client_secret    = local.google_oauth.client_secret
    authorize_scopes = "openid email"
  }

  # email_verified is mapped so the gate can refuse an address Google itself
  # has not verified. username is Google's stable account ID, never the
  # address, which a person can change.
  attribute_mapping = {
    email          = "email"
    email_verified = "email_verified"
    username       = "sub"
  }
}

resource "aws_cloudwatch_log_group" "authgate" {
  name              = "/aws/lambda/scoreboard-authgate"
  retention_in_days = 30
}

resource "aws_iam_role" "authgate" {
  name               = "scoreboard-authgate"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# Its own logs and one parameter. No Cognito permissions at all: the function
# only answers yes or no, and Cognito acts on the answer.
data "aws_iam_policy_document" "authgate" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.authgate.arn}:*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.allowed_emails.arn]
  }
}

resource "aws_iam_role_policy" "authgate" {
  name   = "scoreboard-authgate"
  role   = aws_iam_role.authgate.id
  policy = data.aws_iam_policy_document.authgate.json
}

data "archive_file" "authgate" {
  type        = "zip"
  source_file = "${path.module}/../build/authgate/bootstrap"
  output_path = "${path.module}/../build/authgate.zip"
}

# Cognito gives a trigger five seconds and then fails the sign-in, so a longer
# timeout would buy nothing.
resource "aws_lambda_function" "authgate" {
  function_name    = "scoreboard-authgate"
  role             = aws_iam_role.authgate.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.authgate.output_path
  source_code_hash = data.archive_file.authgate.output_base64sha256
  timeout          = 5
  memory_size      = 128
  environment {
    variables = {
      ALLOWLIST_PARAMETER = aws_ssm_parameter.allowed_emails.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.authgate]
}

# One grant covers both triggers: the same function, called by the same pool.
resource "aws_lambda_permission" "authgate_cognito" {
  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authgate.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.admin.arn
}

# One refused sign-in is somebody's uninvited Google account, or a relative
# signed in to the wrong profile. Three in an hour is somebody trying -- or
# the invite list being unreadable, which refuses everyone, the owner
# included, and logs the same line with a different reason.
resource "aws_cloudwatch_log_metric_filter" "signin_refused" {
  name           = "scoreboard-signin-refused"
  log_group_name = aws_cloudwatch_log_group.authgate.name
  pattern        = "\"sign-in refused\""

  metric_transformation {
    name      = "SignInRefused"
    namespace = "Scoreboard"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "signin_refused" {
  alarm_name          = "scoreboard-signin-refused"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 3
  period              = 3600
  statistic           = "Sum"
  namespace           = "Scoreboard"
  metric_name         = "SignInRefused"
  alarm_description   = <<-EOT
    Three or more refused sign-ins to the admin site within an hour. Read
    /aws/lambda/scoreboard-authgate: each refusal logs its trigger, a reason
    and the email's domain, never the address. "not invited" from assorted
    domains is somebody probing. "invite list unavailable" means nobody at
    all can sign in until the SSM parameter is readable again.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}
```

- [ ] **Step 4: Attach the gate to the pool**

In `terraform/admin.tf`, replace:

```hcl
  # Invite-only: nobody can create their own account. Users are created by the
  # administrator. An open sign-up form on a hobby project is an invitation to
  # abuse it, and this project hands panels to family, not to the public.
  admin_create_user_config {
```

with:

```hcl
  # Invite-only -- but this setting only closes the SignUp operation. It does
  # not stop a Google account's first sign-in from creating a profile; the
  # authgate function in lambda_config below does that, by admitting only
  # invited addresses (signin.tf). This stays true anyway, so the password
  # sign-up path is shut twice rather than once.
  admin_create_user_config {
```

Then replace:

```hcl
  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }
}
```

with:

```hcl
  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration {
    enabled = true
  }

  # Both triggers are the same function, which tells them apart by the
  # event's triggerSource. V1_0 is the token generation event every Cognito
  # feature plan offers; the function changes no claims, so it needs nothing
  # newer.
  lambda_config {
    pre_sign_up = aws_lambda_function.authgate.arn
    pre_token_generation_config {
      lambda_arn     = aws_lambda_function.authgate.arn
      lambda_version = "V1_0"
    }
  }
}
```

- [ ] **Step 5: Make Google the client's only way in**

In `terraform/admin.tf`, replace:

```hcl
# A public client with no secret: the site is static, so a secret would be
# readable by anyone who views source. Authorization code with PKCE is the
# flow that does not need one.
```

with:

```hcl
# A public client with no secret: the site is static, so a secret would be
# readable by anyone who views source. Authorization code with PKCE is the
# flow that does not need one. Google is its only identity provider, so there
# is no password here to phish, guess or reset.
```

Replace:

```hcl
  supported_identity_providers         = ["COGNITO"]

  # SRP proves a password without sending it, and refresh keeps a session
  # alive between the two; nothing here needs USER_PASSWORD_AUTH, which
  # would let a caller submit a password straight to Cognito for guessing.
  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
```

with:

```hcl
  supported_identity_providers         = [aws_cognito_identity_provider.google.provider_name]

  # No password flow of any kind, SRP included: with Google as the only way
  # in, the one thing a caller may do with this client directly is exchange a
  # refresh token. The list cannot simply be left out, because Cognito's
  # default for a client with none is to allow SRP and custom auth.
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]
```

- [ ] **Step 6: Run the tripwires and confirm they pass**

Run: `cd site && node --test tests/signin-config.test.js`
Expected: all six PASS.

- [ ] **Step 7: Format and validate**

Run:

```bash
cd terraform && terraform fmt -check -diff && { test -d .terraform || terraform init -backend=false -input=false >/dev/null; } && terraform validate
```

Expected: `fmt` prints nothing and `Success! The configuration is valid.` If `fmt -check` fails, run `terraform fmt`, review the diff, and rerun. Do **not** run `terraform plan`. It needs AWS credentials and the gitignored tfvars, and the controller runs it in Task 5.

- [ ] **Step 8: Run the site's whole test suite**

Run: `make test-js`
Expected: PASS, the new file included.

- [ ] **Step 9: Commit**

```bash
git add terraform/signin.tf terraform/admin.tf site/tests/signin-config.test.js
git commit -m "terraform: Google as the only way in, behind the invite-list gate

Adds the Google identity provider, the invite list as an SSM parameter
Terraform seeds once and never overwrites, the authgate function with
read access to that one parameter and nothing else, and an alarm on three
refused sign-ins in an hour. The pool calls the gate at sign-up and at
every token issuance; the site's client now supports Google alone and
allows no password flow, SRP included.

One cost is stated rather than hidden: the Google client secret is read
from Secrets Manager but stored in Terraform state by the identity
provider resource.

Tripwire tests, run with the site's suite, fail if the client regains a
password flow or a second provider, if either trigger comes off the pool,
or if the invite list loses its protection from apply."
```

---

### Task 3: The site: straight to Google, clearer refusals, a privacy page

**Files:**
- Modify: `site/assets/auth.js:51-63`
- Modify: `site/tests/auth.test.js:53-64`
- Modify: `site/assets/app.js:316-320`
- Modify: `site/index.html` (the sign-in button and the footer)
- Create: `site/privacy/index.html`
- Create: `site/tests/pages.test.js`

**Interfaces:**
- Consumes: nothing from Tasks 1–2 at runtime. `make site`'s root sync already uploads `site/privacy/`, because it excludes only `assets/`, `tests/`, `package.json` and `config.json`. CloudFront's `index_rewrite` function serves `/privacy/` as `/privacy/index.html`.
- Produces: `https://scoreboard.davidjdrake.com/privacy/`, which Task 5 gives to Google's Branding page.

- [ ] **Step 1: Write the failing tests**

In `site/tests/auth.test.js`, replace:

```js
test("the authorize URL asks for a code with PKCE, for exactly openid and email", () => {
```

with:

```js
test("the authorize URL asks Google for a code with PKCE, for exactly openid and email", () => {
```

and in the same test, after `  assert.equal(p.get("scope"), "openid email");` add:

```js
  // Straight to Google: without it Cognito first shows a page of its own
  // with a single button on it.
  assert.equal(p.get("identity_provider"), "Google");
```

Create `site/tests/pages.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const page = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const index = page("index.html");
let privacy = "";
try {
  privacy = page("privacy/index.html");
} catch {
  // Reported by the tests below rather than as a crash.
}

test("the sign-in button says where it goes", () => {
  assert.match(index, /<button id="sign-in"[^>]*>Sign in with Google<\/button>/);
});

// Google asks that an app's home page link to its privacy policy.
test("the home page links to the privacy policy", () => {
  assert.match(index, /<a href="\/privacy\/">Privacy<\/a>/);
});

test("the privacy page exists and says when it was last updated", () => {
  assert.match(privacy, /<h1>Privacy<\/h1>/);
  assert.match(privacy, /Last updated [A-Z][a-z]+ \d{1,2}, \d{4}\./);
});

// It is served under the same CSP as the admin page and has no reason to
// run anything at all.
test("the privacy page runs no script", () => {
  assert.ok(privacy, "privacy/index.html is missing");
  assert.doesNotMatch(privacy, /<script/i);
  assert.doesNotMatch(privacy, /\son[a-z]+\s*=/i);
});

test("the privacy page loads nothing from another origin", () => {
  assert.ok(privacy, "privacy/index.html is missing");
  for (const [, url] of privacy.matchAll(/<(?:link|img)[^>]*(?:href|src)="([^"]+)"/g)) {
    assert.ok(url.startsWith("/") || url.startsWith("data:"), `loads from elsewhere: ${url}`);
  }
});
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd site && node --test tests/auth.test.js tests/pages.test.js`
Expected: FAIL. `identity_provider` is null, the button reads `Sign in`, there is no privacy link, and the privacy page is missing.

- [ ] **Step 3: Send sign-in straight to Google**

In `site/assets/auth.js`, replace:

```js
    redirect_uri: redirectUri(origin),
    scope: "openid email",
    state,
```

with:

```js
    redirect_uri: redirectUri(origin),
    scope: "openid email",
    // The client supports Google alone. Naming it skips a Cognito page whose
    // only content would be a button that says Google.
    identity_provider: "Google",
    state,
```

- [ ] **Step 4: Update the page copy and the error message**

In `site/index.html`, replace:

```html
    <button id="sign-in" class="btn primary" type="button">Sign in</button>
```

with:

```html
    <button id="sign-in" class="btn primary" type="button">Sign in with Google</button>
```

and replace:

```html
    <p>Part of <a href="https://hockeytrack.davidjdrake.com/">HockeyTrack</a>.</p>
```

with:

```html
    <p>Part of <a href="https://hockeytrack.davidjdrake.com/">HockeyTrack</a>. <a href="/privacy/">Privacy</a></p>
```

In `site/assets/app.js`, replace:

```js
    if (here.searchParams.has("error")) {
      forgetSignIn(sessionStorage);
      showSignedOut("Sign-in was cancelled.");
      return;
    }
```

with:

```js
    if (here.searchParams.has("error")) {
      forgetSignIn(sessionStorage);
      // Cognito sends an error back both when someone cancels at Google and
      // when the gate refuses an uninvited account. They are not told apart:
      // the only clue is error_description, text taken from the URL, and this
      // page does not repeat what a URL tells it to say. One message covers
      // both.
      showSignedOut("Sign-in did not finish. If you cancelled it, sign in again. If you did not, this Google account may not be invited.");
      return;
    }
```

- [ ] **Step 5: Write the privacy page**

Create `site/privacy/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="What the HockeyTrack scoreboard site keeps about the people who sign in to it, and why.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%92%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="/assets/site.css">
<link rel="stylesheet" href="/assets/admin.css">
<title>Privacy · HockeyTrack Scoreboards</title>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<nav class="nav" aria-label="Site">
  <div class="wrap">
    <a class="wordmark" href="/">HOCKEYTRACK</a>
  </div>
</nav>

<main class="wrap" id="main">
  <h1>Privacy</h1>
  <p class="lede">scoreboard.davidjdrake.com lets a few invited people manage HockeyTrack scoreboard panels. It is a personal project run by David Drake, and it keeps as little about you as it can while doing that.</p>

  <section class="card">
    <h2>What Google tells this site</h2>
    <p>When you sign in with Google, this site asks Google for your email address and nothing more. Google sends that address, whether Google has verified it, and an identifier for your Google account. The site never sees your Google password, your name, your photo, your contacts, or anything else in your Google account.</p>
  </section>

  <section class="card">
    <h2>What is kept</h2>
    <ul>
      <li><strong>Your account:</strong> your email address, Google's identifier for your account, and whether the address is verified, held by Amazon Cognito.</li>
      <li><strong>The invite list:</strong> the email addresses allowed to sign in.</li>
      <li><strong>Your panels:</strong> for each panel you claim, the name you give it, the game it follows, and which account owns it.</li>
      <li><strong>A panel waiting to be claimed:</strong> if its setup file names an owner, a one-way hash of that address, which expires after a day.</li>
      <li><strong>Service logs:</strong> requests to the site's API are logged with the requesting IP address and your account identifier, and deleted after 30 days. A refused sign-in is logged with only the domain of the email address, such as gmail.com.</li>
    </ul>
  </section>

  <section class="card">
    <h2>What is not done with it</h2>
    <p>Information received from Google is used only to sign you in and to work out which panels are yours, in line with the Google API Services User Data Policy. None of it is sold, shared, or used for advertising. The site has no analytics and sets no cookies of its own. Signing in does set cookies on Google's and Amazon Cognito's sign-in pages, which they use to keep you signed in. While you sign in, this page keeps a short-lived code in your browser's session storage, which is cleared when you close the tab.</p>
  </section>

  <section class="card">
    <h2>Who handles it</h2>
    <p>The site runs on Amazon Web Services in the United States, and Google handles signing in. No one else receives any of it.</p>
  </section>

  <section class="card">
    <h2>Removing your information</h2>
    <p>Ask the person who invited you, and your account, your place on the invite list, and your ownership of any panels will be deleted. Logs age out within 30 days. For anything else, open an issue at <a href="https://github.com/DavidJDrake/hockeytrack-scoreboard/issues">github.com/DavidJDrake/hockeytrack-scoreboard</a>, where the site's source code is public. Leave your email address out of the issue.</p>
  </section>

  <p>Last updated September 13, 2026.</p>
</main>

<footer class="site-foot">
  <div class="wrap">
    <p>Part of <a href="https://hockeytrack.davidjdrake.com/">HockeyTrack</a>.</p>
  </div>
</footer>
</body>
</html>
```

The GitHub link is an `<a href>`, not a resource the page loads, so the "loads nothing from another origin" test correctly ignores it.

- [ ] **Step 6: Run the site's tests and confirm they pass**

Run: `make test-js`
Expected: PASS, including `auth.test.js`, `pages.test.js` and `imports.test.js`.

- [ ] **Step 7: Commit**

```bash
git add site/assets/auth.js site/tests/auth.test.js site/assets/app.js site/index.html site/privacy/index.html site/tests/pages.test.js
git commit -m "site: sign in with Google, say so, and publish a privacy policy

Sign in now goes straight to Google rather than through a Cognito page
with one button on it, and the button says where it goes.

A sign-in that comes back with an error now says it may mean the Google
account is not invited, not only that it was cancelled. The page cannot
tell the two apart without repeating text from the URL, which it does
not do.

The privacy page is what Google requires before the app can be
published. It states what the stack actually keeps, down to the 30-day
log retention and the domain-only refusal logs, and it runs no script."
```

---

### Task 4: Documentation

**Files:**
- Modify: `docs/admin-api.md` (the sign-up paragraph and the token walkthrough)
- Modify: `docs/hardware-checks.md` (H8's prerequisites, step 4, step 8)
- Modify: `docs/superpowers/specs/2026-09-07-admin-site-design.md` (§3.1)
- Modify: `docs/superpowers/specs/2026-09-12-device-enrollment-design.md` (§10.1)

**Interfaces:**
- Consumes: the names from Tasks 1–3: `/scoreboard/allowed-emails`, `scoreboard-authgate`, `terraform/signin.tf`, `identity_provider=Google`.
- Produces: nothing code depends on.

- [ ] **Step 1: `docs/admin-api.md`, who can get a token**

Replace:

```markdown
Sign-up is closed. There is no self-registration endpoint or hosted sign-up
flow — users are created by the administrator in the Cognito console
(`aws_cognito_user_pool.admin`, `admin_create_user_config.allow_admin_create_user_only
= true`). If you don't already have an account, the API can't give you one.
```

with:

```markdown
Sign-up is closed. The only way in is Sign in with Google, and only for an
address on the invite list: the SSM parameter `/scoreboard/allowed-emails`,
which the `scoreboard-authgate` function checks when Cognito is about to
create an account and again every time it issues tokens
(`terraform/signin.tf`; design in
`docs/superpowers/specs/2026-09-13-google-sign-in-design.md`). An uninvited
Google account gets no account and no token, so there is nothing for the API
to authorize. Inviting someone is one `aws ssm put-parameter` call, given in
section 4.2 of that spec.
```

- [ ] **Step 2: `docs/admin-api.md`, the walkthrough**

Replace:

```markdown
The client is a public Cognito app client (no secret — anything shipped to a
browser can't keep one), using Authorization Code with PKCE against the
hosted UI. There's no token endpoint you can hit with a single `curl` and a
password; a human has to sign in through the hosted UI once per session. The
```

with:

```markdown
The client is a public Cognito app client (no secret — anything shipped to a
browser can't keep one), using Authorization Code with PKCE against the
hosted UI, with Google as its only identity provider. There is no password to
send anywhere, so there is no token endpoint you can hit with a single `curl`;
a human signs in with an invited Google account once per session. The
```

Replace:

```bash
# 2. Open this URL in a browser and sign in. It redirects to
#    http://localhost:8000/?code=... on success.
echo "https://${USER_POOL_DOMAIN}.auth.${REGION}.amazoncognito.com/login?client_id=${CLIENT_ID}&response_type=code&scope=openid+email&redirect_uri=http://localhost:8000/&code_challenge_method=S256&code_challenge=${CODE_CHALLENGE}"
```

with:

```bash
# 2. Open this URL in a browser and sign in with Google. It redirects to
#    http://localhost:8000/?code=... on success.
echo "https://${USER_POOL_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/authorize?identity_provider=Google&client_id=${CLIENT_ID}&response_type=code&scope=openid+email&redirect_uri=http://localhost:8000/&code_challenge_method=S256&code_challenge=${CODE_CHALLENGE}"
```

- [ ] **Step 3: `docs/hardware-checks.md`, H8**

Replace:

```markdown
as soon as this plan is installed on a Pi. H8 needs the enrollment path this
plan builds, plus a Cognito user to claim with.
```

with:

```markdown
as soon as this plan is installed on a Pi. H8 needs the enrollment path this
plan builds, plus two invited Google accounts: the owner's, and a second one
for step 4.
```

Replace:

```markdown
4. Sign in at https://scoreboard.davidjdrake.com **as a different invited
   user** and type the code into *Claim a panel*. Expect "No panel is waiting
```

with:

```markdown
4. Sign in at https://scoreboard.davidjdrake.com **as a different invited
   user** — a second Google account, added to the invite list for this check
   and taken off it afterwards — and type the code into *Claim a panel*.
   Expect "No panel is waiting
```

Replace:

```markdown
8. Sign in, then leave the tab open and idle for over an hour before touching
   a panel control. Expect a fresh sign-in through Cognito's hosted UI, not a
   broken page — Cognito's hosted-UI session cookie is roughly as long-lived
   as the ID token, about one hour, and the refresh token that could silently
   extend it is discarded by design. This is also the first real observation
```

with:

```markdown
8. Sign in, then leave the tab open and idle for over an hour before touching
   a panel control. Expect a trip back through Cognito and Google, not a
   broken page. Google usually returns at once without asking anything, though
   it may show its account chooser. Cognito's hosted-UI session cookie is
   roughly as long-lived as the ID token, about one hour, and the refresh
   token that could silently extend it is discarded by design. This is also
   the first real observation
```

Then read the lines that follow the replaced step 8. If the next line now begins mid-sentence with odd wrapping (`   of whether API Gateway's …`), leave it: Markdown joins continuation lines, and rewrapping the whole paragraph only adds diff noise.

- [ ] **Step 4: Note the amendment in the two earlier specs**

In `docs/superpowers/specs/2026-09-07-admin-site-design.md`, replace:

```markdown
### 3.1 Identity — Cognito user pool, hosted UI

```

with:

```markdown
### 3.1 Identity — Cognito user pool, hosted UI

> **Amended 2026-09-13** by `2026-09-13-google-sign-in-design.md`. People now
> sign in with Google only. Invite-only is enforced by an allowlist that a
> Cognito trigger checks, because the pool's admin-only setting does not stop
> a federated account from being created.

```

In `docs/superpowers/specs/2026-09-12-device-enrollment-design.md`, replace:

```markdown
### 10.1 Tokens in the browser

```

with:

```markdown
### 10.1 Tokens in the browser

> **Amended 2026-09-13** by `2026-09-13-google-sign-in-design.md`. The hosted
> UI now hands sign-in to Google, so a silent re-authentication passes through
> Google's session as well as Cognito's. Nothing else in this section changes.

```

- [ ] **Step 5: Check for anything else that still describes passwords**

Run: `grep -rn -i "hosted ui\|password\|admin-create-user\|Cognito user\b" docs/admin-api.md docs/hardware-checks.md README.md`
Expected: the remaining hits are about Wi-Fi passwords, the pairing flow, or the hosted UI in its still-true sense, which is Cognito's domain handling the redirect. If any hit still says users are created by an administrator or sign in with a password, fix it in the same style and name it in your report.

- [ ] **Step 6: Commit**

```bash
git add docs/admin-api.md docs/hardware-checks.md docs/superpowers/specs/2026-09-07-admin-site-design.md docs/superpowers/specs/2026-09-12-device-enrollment-design.md
git commit -m "docs: describe Google sign-in and the invite list

The API guide said accounts are created by the administrator; they now
come from an invited Google account's first sign-in, and its token
walkthrough sends people to Google. H8 needs a second invited Google
account for its wrong-user step. The two earlier specs point to the one
that amends them."
```

---

### Task 5: Deploy and verify on the real stack (controller and user, not a subagent)

This task runs after the branch's final review and before it merges, so any fix the real stack forces lands in the same pull request. Run it from the main checkout, `/home/jay/projects/hockeytrack-scoreboard`, with this branch checked out. Build paths are part of a Go binary's build ID, so building in a worktree would show every other function as changed.

The controller does every read-only step and hands the user exact commands for every write, which the user runs with `!`. The owner's address appears only in the gitignored tfvars and in commands typed into the session, never in a committed file. Nobody prints the Google client secret: every `describe-identity-provider` call below carries a `--query` that names the keys it returns, and none of them is `client_secret`.

- [ ] **Step 1: Seed the invite list's first value**

The controller writes `terraform/terraform.tfvars` containing one line, `invited_emails = ["<the owner's address>"]`, using the address the owner gave in conversation. Then run `git check-ignore -v terraform/terraform.tfvars`. Expected: it names the `terraform/*.tfvars` rule in `.gitignore`. If it does not, stop.

- [ ] **Step 2: The user deletes the password account**

Hand the user this command, with the owner's address filled in:
`! aws cognito-idp admin-delete-user --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --username <owner's address>`
Verify read-only: `aws cognito-idp list-users --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --query 'length(Users)'`. Expected: `0`.

- [ ] **Step 3: Test, build, plan**

Run: `make test build`, then `cd terraform && terraform plan -input=false -out=<scratchpad>/signin.tfplan`.

Expected summary: **9 to add, 2 or more to change, 0 to destroy.** The additions are `aws_ssm_parameter.allowed_emails`, `aws_cognito_identity_provider.google`, `aws_cloudwatch_log_group.authgate`, `aws_iam_role.authgate`, `aws_iam_role_policy.authgate`, `aws_lambda_function.authgate`, `aws_lambda_permission.authgate_cognito`, `aws_cloudwatch_log_metric_filter.signin_refused` and `aws_cloudwatch_metric_alarm.signin_refused`. The changes are `aws_cognito_user_pool.admin` (adds `lambda_config`, in place) and `aws_cognito_user_pool_client.site` (in place).

The other four functions may also show `source_code_hash` updates, because `go.mod` changed. That is acceptable only if nothing but the code hash and its derived attributes changes. Any replacement (`-/+`), any destroy, or any change to the pool beyond `lambda_config` is a stop-and-investigate. Read the plan with `terraform show <scratchpad>/signin.tfplan`, never with `-json`.

- [ ] **Step 4: The user applies the saved plan**

Hand the user: `! cd /home/jay/projects/hockeytrack-scoreboard/terraform && terraform apply <scratchpad>/signin.tfplan`

- [ ] **Step 5: Confirm there is no drift**

Run: `cd terraform && terraform plan -input=false -detailed-exitcode`. Expected: exit 0, `No changes.`

If it shows a perpetual change to `aws_cognito_identity_provider.google`'s `provider_details`, Cognito has added keys of its own. List their **names only** with `aws cognito-idp describe-identity-provider --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --provider-name Google --query 'keys(IdentityProvider.ProviderDetails)'`. Then fetch the values of the added keys by naming each one in a multi-select, and nothing else: `aws cognito-idp describe-identity-provider --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --provider-name Google --query 'IdentityProvider.ProviderDetails.{authorize_url:authorize_url,token_url:token_url}'`, with the key names the first command printed in place of those two. Never `client_id`, never `client_secret`, never `ProviderDetails` whole and never a wildcard. Add them to `provider_details` in `signin.tf` and replan until clean. Commit that as its own change, explaining why.

If the pool shows a perpetual `pre_token_generation` diff alongside `pre_token_generation_config`, handle it the same way: describe it read-only, record the ruling, fix it, commit.

- [ ] **Step 6: Confirm the deployed configuration, not just the Terraform text**

A clean plan proves the state matches the code; these read the settings back from Cognito and Lambda themselves. All read-only. The client ID comes from `terraform output -raw user_pool_client_id`, run in `terraform/`.

1. `aws cognito-idp describe-user-pool-client --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --client-id <client ID> --query 'UserPoolClient.{flows:ExplicitAuthFlows,idps:SupportedIdentityProviders,write:WriteAttributes,scopes:AllowedOAuthScopes}'`
   Expected: `flows` is exactly `["ALLOW_REFRESH_TOKEN_AUTH"]`; `idps` is exactly `["Google"]`; `write` is exactly `["email"]` (Cognito rejects `email_verified` there); `scopes` holds `openid` and `email` and nothing else. Order within a list does not matter.
2. `aws cognito-idp describe-user-pool --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --query 'UserPool.{lambda:LambdaConfig,admin:AdminCreateUserConfig}'`
   Expected: `lambda` names the `scoreboard-authgate` ARN as `PreSignUp`, and as `PreTokenGenerationConfig.LambdaArn` with `LambdaVersion` `V1_0` (Cognito may also echo it as `PreTokenGeneration`); no other trigger. `admin.AllowAdminCreateUserOnly` is `true`.
3. `aws cognito-idp list-user-pool-clients --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --query 'length(UserPoolClients)'`
   Expected: `1`. A second client would be a second door the first one's settings do not govern.
4. `aws cognito-idp describe-identity-provider --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --provider-name Google --query 'IdentityProvider.{type:ProviderType,mapping:AttributeMapping,scopes:ProviderDetails.authorize_scopes}'`
   Expected: `type` `Google`; `mapping` maps `email`, `email_verified` and `username` (to `sub`); `scopes` `openid email`.
5. `aws lambda get-policy --region us-east-1 --function-name scoreboard-authgate --query Policy --output text | python3 -c 'import json,sys; p=json.load(sys.stdin); print(len(p["Statement"])); [print(s["Principal"], s.get("Condition")) for s in p["Statement"]]'`
   Expected: `1`, then one statement whose principal is `{'Service': 'cognito-idp.amazonaws.com'}` and whose condition is an `ArnLike` `AWS:SourceArn` equal to the pool's ARN, `arn:aws:cognito-idp:us-east-1:<account>:userpool/us-east-1_xJ6aWqZfR`.

Any difference is a stop-and-investigate. Record the results for Step 13.

- [ ] **Step 7: The user publishes the site; the controller checks it**

Hand the user: `! cd /home/jay/projects/hockeytrack-scoreboard && make site`

Verify: `curl -sI https://scoreboard.davidjdrake.com/privacy/` shows `200`, `content-type: text/html` and the `content-security-policy` header. Then `curl -s https://scoreboard.davidjdrake.com/ | grep -c 'Sign in with Google'` prints `1`.

- [ ] **Step 8: Spec §8 test 1, the owner signs in**

The owner signs in at https://scoreboard.davidjdrake.com with Google. Expected: the page shows their address and loads the panel list without an error, which proves an authorized API call succeeded. The **Download setup file** button is enabled, with no "Verify your email address before setting up a panel." note beside it. That note is what the site shows when the ID token's `email_verified` is not true (`renderAdd` in `site/assets/app.js`), so its absence is the site's evidence that the token carries a verified email. The owner-hint check on the server (`ownerMatches` in `cloud/cmd/enroll/handler.go`) is not exercised here; only hardware check H8 exercises it.

Verify read-only: `aws cognito-idp list-users --region us-east-1 --user-pool-id us-east-1_xJ6aWqZfR --query 'Users[].{user:Username,status:UserStatus,verified:Attributes[?Name==\`email_verified\`]|[0].Value}'`. Expected: one user, a `Google_…` username, status `EXTERNAL_PROVIDER`, verified `true`.

Then `aws logs tail /aws/lambda/scoreboard-authgate --region us-east-1 --since 15m | grep -c 'reason="malformed event"'`. Expected: `0`. Any such line means Cognito sent an event the gate could not decode, even if the sign-in that followed succeeded.

If sign-in fails, read `aws logs tail /aws/lambda/scoreboard-authgate --region us-east-1 --since 15m` before changing anything:
- `reason="email not verified by Google"` means the `email_verified` mapping is not reaching the trigger as `"true"`. Record what the event actually carries, which the log does not show, by adding a temporary log of the attribute's value only, never the address. Then rule on the fix.
- `reason="malformed event"` means the gate could not decode Cognito's event. Record the trigger source and the decode error, never the event body, which carries the address. Then rule on the fix.
- No refusal logged, with Cognito's own error in the redirect mentioning sign-up or admin-only creation, means `allow_admin_create_user_only` blocks federated creation. That is the §4.3 fallback: set it to `false`, with the trigger as the gate, and record it as a finding in the spec. It is a security-relevant change, so confirm it with the user first.

- [ ] **Step 9: Spec §8 test 5, native sign-up and every password path are refused**

1. Hand the user: `! aws cognito-idp sign-up --region us-east-1 --client-id "$(cd /home/jay/projects/hockeytrack-scoreboard/terraform && terraform output -raw user_pool_client_id)" --username probe@example.com --password 'Probe-only-never-used-1'`
   Expected: `NotAuthorizedException` saying SignUp is not permitted. If Step 8 forced `allow_admin_create_user_only` to `false`, the expectation instead is a `UserLambdaValidationException` carrying the gate's refusal, and exactly one new authgate log line, `trigger=PreSignUp_SignUp reason="not a Google sign-in" domain=example.com`. Either way, confirm with `list-users` (Step 8's command) that there is still exactly one user.
2. Hand the user these three, one at a time, each with the same client ID:
   `! aws cognito-idp initiate-auth --region us-east-1 --client-id <client ID> --auth-flow USER_PASSWORD_AUTH --auth-parameters USERNAME=probe@example.com,PASSWORD=Probe-only-never-used-1`
   `! aws cognito-idp initiate-auth --region us-east-1 --client-id <client ID> --auth-flow USER_SRP_AUTH --auth-parameters USERNAME=probe@example.com,SRP_A=00`
   `! aws cognito-idp initiate-auth --region us-east-1 --client-id <client ID> --auth-flow USER_AUTH --auth-parameters USERNAME=probe@example.com`
   Expected: each fails with an error saying that flow is not enabled for this client. Record each exact error code and message. An error about the user, the password or a challenge instead means Cognito looked past the flow check: stop and investigate before calling that path closed. An error about the pool's feature plan (`USER_AUTH` needs Essentials or Plus) also leaves the flow closed; record it as such.
3. Probe the hosted pages, read-only. Set `DOMAIN=$(python3 -c 'import json; print(json.load(open("site/config.json"))["cognitoDomain"])')` from the repository root, and `Q="client_id=<client ID>&response_type=code&redirect_uri=https%3A%2F%2Fscoreboard.davidjdrake.com%2F"`. Then:
   `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "https://$DOMAIN/login?$Q"`
   `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "https://$DOMAIN/signup?$Q"`
   `curl -s "https://$DOMAIN/login?$Q" | grep -ci 'type="password"'`
   `curl -s "https://$DOMAIN/signup?$Q" | grep -ci 'type="password"'`
   Record the status and redirect each page returns; there is no fixed expectation for those. Expected for both `grep -ci` lines: `0`, so no password field is served for this client.

- [ ] **Step 10: Spec §8 test 3, removal ends access**

1. Save the current list without printing it: `aws ssm get-parameter --region us-east-1 --name /scoreboard/allowed-emails --query Parameter.Value --output text | tr -d '\n' > <scratchpad>/allowlist-before.txt && chmod 600 <scratchpad>/allowlist-before.txt`
2. The owner signs in and leaves that tab open.
3. Hand the user: `! aws ssm put-parameter --region us-east-1 --name /scoreboard/allowed-emails --type String --overwrite --value removed@example.invalid`
4. Wait 70 seconds. **The realistic case first:** the owner does not sign out, and reloads the open tab. The site sends the browser back through sign-in on its own, using the sessions Cognito and Google still hold, so new tokens are requested without the owner signing in again. If Google shows an account chooser, the owner picks their account; record that it appeared. Expected: refused. Accept either "Sign-in did not finish…" (Cognito redirected back with an error) or "Sign-in could not be completed…" (the token exchange failed). Record which message appeared, and the trigger source on the authgate log's refusal line, `reason="not invited" domain=<owner's domain>`.
5. **Then the sign-out case:** the owner signs out, then signs in. Expected: refused. On 2026-09-14 the page showed "Sign-in could not be completed…" in both cases, because the token check runs when the site exchanges its code, after Cognito and Google have already redirected back; and the authgate log shows `trigger=TokenGeneration_HostedAuth reason="not invited" domain=<owner's domain>`.
6. Hand the user: `! aws ssm put-parameter --region us-east-1 --name /scoreboard/allowed-emails --type String --overwrite --value "file://<scratchpad>/allowlist-before.txt"`
7. Wait 70 seconds. The owner signs in. Expected: admitted. Then delete `<scratchpad>/allowlist-before.txt`.
8. Learn whether invite-list values reach the audit log, printing key names and never a value. CloudTrail can take several minutes to show an event, so if nothing matches, wait five minutes and rerun:
   ```bash
   aws cloudtrail lookup-events --region us-east-1 \
     --lookup-attributes AttributeKey=EventName,AttributeValue=PutParameter \
     --max-results 10 --query 'Events[].CloudTrailEvent' --output json |
   python3 -c 'import json,sys
   for raw in json.load(sys.stdin):
       e = json.loads(raw); rp = e.get("requestParameters") or {}
       if rp.get("name") == "/scoreboard/allowed-emails":
           v = rp.get("value")
           print(e["eventTime"], sorted(rp.keys()), "no value key" if v is None else ("value masked" if str(v).startswith("HIDDEN") else "value in clear"))'
   ```
   Expected: one or more lines for this step's `put-parameter` calls. Record what they show. If `value` appears in clear, record that in §9: the audit log then holds every invited and removed address for its full retention. The privacy page's audit-log bullet already says changes to the invite list "may include the email address involved"; confirm that sentence is still on the page, and keep it even if the value is masked, because other account changes have not been checked.

- [ ] **Step 11: Complete Google's Branding page and publish**

In Google Cloud console, project `hockeytrack-scoreboard`, open **Google Auth Platform → Branding**:
- Application home page: `https://scoreboard.davidjdrake.com/`
- Application privacy policy link: `https://scoreboard.davidjdrake.com/privacy/`
- Authorized domains: `davidjdrake.com` and Cognito's full hostname, `scoreboard-admin-989232581535.auth.us-east-1.amazoncognito.com` (not `amazoncognito.com`: `auth.us-east-1.amazoncognito.com` is a public suffix)

Save. Then open **Audience → Publish app** and confirm. The app requests only `openid` and `email`, which Google treats as non-sensitive scopes, so publishing should not require verification. If Google asks for verification anyway, stop and report what it asks for.

- [ ] **Step 12: Spec §8 tests 2 and 4, a stranger is refused and the alarm fires**

The alarm sums refusals over a 3600-second period. On 2026-09-14 it evaluated a rolling hour, not a clock hour (its datapoint window ran 21:14–22:14 UTC), so Step 10's refusals may already have fired it. If the alarm is already in `ALARM` with its email received, test 4 has passed and one stranger attempt is enough for test 2.

1. The user signs in with a Google account that is **not** on the invite list. Use a private window, so the owner's Google session is not reused. Expected: the refusal message on the page; the log shows `trigger=PreSignUp_ExternalProvider reason="not invited"` and the account's domain; `list-users` still shows exactly one user.
2. The user repeats that attempt twice more from the same private window, inside the same clock hour. Expected: three refusal lines from this step in `aws logs tail /aws/lambda/scoreboard-authgate --region us-east-1 --since 30m`.
3. Check it: `aws cloudwatch describe-alarms --region us-east-1 --alarm-names scoreboard-signin-refused --query 'MetricAlarms[].{state:StateValue,reason:StateReason}'`. Expected: `ALARM` within a few minutes, and the security-alerts email arrives. If it is still `OK` after fifteen minutes, check the metric itself: `aws cloudwatch get-metric-statistics --region us-east-1 --namespace Scoreboard --metric-name SignInRefused --statistics Sum --period 3600 --start-time <the start of this clock hour, ISO 8601> --end-time <now>`. Also record whether Cognito retried any refused trigger, which would show as more refusal lines than attempts.

- [ ] **Step 13: Record what the real stack showed**

Append a section `## 9. Verification record (2026-09-13)` to `docs/superpowers/specs/2026-09-13-google-sign-in-design.md`. Give each test in §8 its outcome and its evidence: log lines with the domain only, the `list-users` shape, the alarm state. Include every ruling made in Steps 5, 8 and 12. State these plainly:
- whether `allow_admin_create_user_only` interfered, which answers fact 1's inference;
- that the refresh-token path of the token check was not exercised live, because the site discards refresh tokens by design;
- that the server-side owner-hint check was not exercised, and waits for hardware check H8; Step 8 showed only the site's own `email_verified` evidence;
- which message the no-sign-out removal in Step 10 produced, and the trigger source its refusal logged;
- the deployed-configuration results from Step 6;
- the closed-path probe results from Step 9: each `initiate-auth` error, what `/login` and `/signup` returned, and the password-field counts;
- whether the audit log holds invite-list values, from Step 10.

Commit it: `docs: record what the real stack showed for Google sign-in`.

---

## Follow-ups this plan deliberately does not do

- **Alarm-modification coverage.** HockeyTrack's `hockeytrack-sec-alerting-modification` rule watches alarms by the `scoreboard-iot-` prefix, so neither `scoreboard-signin-refused` nor the three `scoreboard-enroll-*` alarms page if rewritten or deleted. Widening that rule is a change in the other repository.
- **LitLibrary's revocation gap.** Its trigger checks the invite list only at sign-up, so removing someone there leaves their account working. It deserves a ticket in that project.
- **The pool's password settings** (`password_policy`, `account_recovery_setting`, TOTP MFA) now govern nothing, but they are left in place. Removing them buys nothing, and `username_attributes`, which sits beside them, forces replacement if touched.
- **Nothing pages on a change to the gate.** Writes to `/scoreboard/allowed-emails`, to the pool's `lambda_config`, clients or identity providers, and to the `scoreboard-authgate` function raise no alert. An `UpdateUserPool` that drops the triggers would fail open to every Google account, silently. The fix is an EventBridge rule on those management events, sending to the security-alerts topic, alongside HockeyTrack's account security rules.
- **Gate failures that log nothing.** A crash or timeout refuses the sign-in without a `sign-in refused` line, so `scoreboard-signin-refused` never counts it. Add an alarm on `scoreboard-authgate`'s Lambda `Errors` metric, with `Throttles` beside it, because Lambda counts a throttled invocation as neither an invocation nor an error.
