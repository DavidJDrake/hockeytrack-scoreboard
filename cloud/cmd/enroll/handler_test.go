package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/enroll"
)

func csrPEM(t *testing.T) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: "ignored"},
	}, key)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}))
}

func newHandler() (*Handler, *enroll.FakeIssuer) {
	issuer := &enroll.FakeIssuer{CertPEM: "CERTPEM"}
	return &Handler{
		Enrollments: enroll.NewFake(),
		Devices:     devices.NewFake(),
		Issuer:      issuer,
		IoTEndpoint: "a1.iot.us-east-1.amazonaws.com",
		TTL:         24 * time.Hour,
		CodeTTL:     15 * time.Minute,
	}, issuer
}

func post(path, body string) events.APIGatewayV2HTTPRequest {
	return events.APIGatewayV2HTTPRequest{
		RouteKey: "POST " + path,
		Body:     body,
	}
}

func submit(t *testing.T, h *Handler) (code, token string) {
	t.Helper()
	return submitFor(t, h, "")
}

// submitFor enrolls with an optional owner hint, the way a card prepared by
// the site would.
func submitFor(t *testing.T, h *Handler, owner string) (code, token string) {
	t.Helper()
	payload := `{"csr":` + strconv.Quote(csrPEM(t))
	if owner != "" {
		payload += `,"owner":` + strconv.Quote(owner)
	}
	payload += `}`
	resp, err := h.Handle(context.Background(), post("/api/enroll", payload))
	if err != nil || resp.StatusCode != 201 {
		t.Fatalf("submit: status %d err %v body %s", resp.StatusCode, err, resp.Body)
	}
	var out struct {
		Code  string `json:"code"`
		Token string `json:"token"`
	}
	if err := json.Unmarshal([]byte(resp.Body), &out); err != nil {
		t.Fatal(err)
	}
	return out.Code, out.Token
}

// claimAs lets a test supply exactly the claims a token would carry, for
// cases that need to control email_verified rather than get it for free.
func claimAs(h *Handler, code string, claims map[string]string) events.APIGatewayV2HTTPResponse {
	req := post("/api/devices/claim", `{"code":"`+code+`"}`)
	req.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{Claims: claims},
	}
	resp, _ := h.Handle(context.Background(), req)
	return resp
}

func claim(h *Handler, code, sub string) events.APIGatewayV2HTTPResponse {
	return claimAs(h, code, map[string]string{
		"sub": sub, "email": sub + "@example.com", "email_verified": "true",
	})
}

func collect(h *Handler, token string) events.APIGatewayV2HTTPResponse {
	req := events.APIGatewayV2HTTPRequest{
		RouteKey: "GET /api/enroll",
		Headers:  map[string]string{"authorization": "Bearer " + token},
	}
	resp, _ := h.Handle(context.Background(), req)
	return resp
}

func TestSubmitReturnsACodeAndAToken(t *testing.T) {
	h, issuer := newHandler()
	code, token := submit(t, h)
	if len(code) != 8 || token == "" {
		t.Fatalf("code %q token %q", code, token)
	}
	// The whole invariant: submitting mints nothing.
	if len(issuer.Calls) != 0 {
		t.Errorf("a certificate was issued before anyone claimed: %v", issuer.Calls)
	}
}

// TestSubmitStoresHashesNotSecrets proves the "a dump of the enrollments
// table yields nobody a usable token and nobody a claimable code" property
// actually holds, rather than just that both ends of each hash agree with
// each other. It reaches into the store the way an attacker with a dump
// would: by the stored fields, not by re-deriving what was sent.
func TestSubmitStoresHashesNotSecrets(t *testing.T) {
	h, _ := newHandler()
	owner := "owner-1@example.com"
	code, token := submitFor(t, h, owner)

	p, found, err := h.Enrollments.ByTokenHash(context.Background(), enroll.HashSecret(token))
	if err != nil || !found {
		t.Fatalf("stored enrollment not found: found=%v err=%v", found, err)
	}
	if p.CodeHash == code {
		t.Error("CodeHash holds the plaintext code")
	}
	if p.TokenHash == token {
		t.Error("TokenHash holds the plaintext token")
	}
	if p.OwnerHintHash == owner {
		t.Error("OwnerHintHash holds the plaintext owner hint")
	}
	if want := enroll.HashSecret(enroll.NormalizeOwner(owner)); p.OwnerHintHash != want {
		t.Errorf("OwnerHintHash = %q, want %q (hash of the normalized owner)", p.OwnerHintHash, want)
	}
}

func TestSubmitRejectsRubbish(t *testing.T) {
	h, _ := newHandler()
	for name, body := range map[string]string{
		"not json":  `{`,
		"no csr":    `{}`,
		"not a csr": `{"csr":"hello"}`,
	} {
		resp, _ := h.Handle(context.Background(), post("/api/enroll", body))
		if resp.StatusCode != 400 {
			t.Errorf("%s returned %d, want 400", name, resp.StatusCode)
		}
	}
}

func TestCollectingBeforeAnyoneClaimsIsAccepted(t *testing.T) {
	h, _ := newHandler()
	_, token := submit(t, h)
	if got := collect(h, token).StatusCode; got != 202 {
		t.Errorf("collect before claim returned %d, want 202", got)
	}
}

func TestTheWholeLoop(t *testing.T) {
	h, issuer := newHandler()
	code, token := submit(t, h)

	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Fatalf("claim returned %d", got)
	}
	if len(issuer.Calls) != 1 {
		t.Fatalf("issuer was called %d times", len(issuer.Calls))
	}

	resp := collect(h, token)
	if resp.StatusCode != 200 {
		t.Fatalf("collect returned %d: %s", resp.StatusCode, resp.Body)
	}
	var out struct {
		CertificatePem string `json:"certificatePem"`
		ThingName      string `json:"thingName"`
		Endpoint       string `json:"endpoint"`
	}
	if err := json.Unmarshal([]byte(resp.Body), &out); err != nil {
		t.Fatal(err)
	}
	if out.CertificatePem != "CERTPEM" || !strings.HasPrefix(out.ThingName, "scoreboard-") || out.Endpoint == "" {
		t.Errorf("collect returned %+v", out)
	}
}

func TestCollectingTwiceFails(t *testing.T) {
	// The row is deleted on collection. After that the certificate exists only
	// on the device and in AWS IoT.
	h, _ := newHandler()
	code, token := submit(t, h)
	claim(h, code, "owner-1")
	if got := collect(h, token).StatusCode; got != 200 {
		t.Fatal("first collect failed")
	}
	if got := collect(h, token).StatusCode; got != 404 {
		t.Errorf("second collect returned %d, want 404", got)
	}
}

func TestAWrongTokenIs404NotForbidden(t *testing.T) {
	// A wrong token and a nonexistent one must look the same, so nobody can
	// probe for which tokens exist.
	h, _ := newHandler()
	submit(t, h)
	if got := collect(h, "definitely-not-a-token").StatusCode; got != 404 {
		t.Errorf("got %d, want 404", got)
	}
}

func TestClaimingRequiresAuthentication(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submit(t, h)
	resp, _ := h.Handle(context.Background(), post("/api/devices/claim", `{"code":"`+code+`"}`))
	if resp.StatusCode != 401 {
		t.Errorf("unauthenticated claim returned %d, want 401", resp.StatusCode)
	}
	if len(issuer.Calls) != 0 {
		t.Error("an unauthenticated caller caused a certificate to be issued")
	}
}

func TestClaimingTwiceFails(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submit(t, h)
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Fatal("first claim failed")
	}
	if got := claim(h, code, "owner-2").StatusCode; got != 404 {
		t.Errorf("second claim returned %d, want 404", got)
	}
	if len(issuer.Calls) != 1 {
		t.Errorf("issuer was called %d times; the second claim must not mint", len(issuer.Calls))
	}
}

// TestAnIssueFailureLeavesAnOwnedDeviceRowForRecovery pins the claim
// ordering: Register and Claim run before Issue, so a failure in Issue (or
// SetCertificate) leaves the owner a row they can see and unbind, which is
// the recovery path the spec describes. The reverse order -- Issue first --
// would leave an active certificate, thing and attached policy in AWS with
// no devices row at all: nothing to find, nothing to unbind.
func TestAnIssueFailureLeavesAnOwnedDeviceRowForRecovery(t *testing.T) {
	h, issuer := newHandler()
	issuer.Err = errors.New("iot is down")
	code, _ := submit(t, h)

	if got := claim(h, code, "owner-1").StatusCode; got != 500 {
		t.Fatalf("claim with a failing issuer returned %d, want 500", got)
	}
	devs, err := h.Devices.ListByOwner(context.Background(), "owner-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(devs) != 1 {
		t.Fatalf("owner has %d devices after a failed issue, want 1 (recoverable via unbind)", len(devs))
	}
}

func TestClaimingAnUnknownCodeIs404(t *testing.T) {
	h, _ := newHandler()
	if got := claim(h, "ZZZZ2222", "owner-1").StatusCode; got != 404 {
		t.Errorf("got %d, want 404", got)
	}
}

func TestAPreBoundPanelRefusesEverybodyElse(t *testing.T) {
	// The shoulder-surfing case, and the reason the owner hint exists. Somebody
	// who reads the code off the screen has an invited account of their own --
	// and still cannot use what they saw.
	h, issuer := newHandler()
	code, _ := submitFor(t, h, "owner-1@example.com")

	if got := claim(h, code, "owner-2").StatusCode; got != 404 {
		t.Errorf("a stranger's claim returned %d, want 404", got)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("a stranger's claim minted a certificate")
	}
	// And the real owner is not locked out by the failed attempt: the one-time
	// guard must not have been consumed.
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the owner's claim returned %d, want 200", got)
	}
}

func TestAPreBoundPanelMatchesItsOwnerLoosely(t *testing.T) {
	// The hint is typed into a file by hand; the claim arrives in a JWT. Case
	// and whitespace must not decide who owns a panel.
	h, _ := newHandler()
	code, _ := submitFor(t, h, "  Owner-1@Example.COM ")
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the owner's claim returned %d, want 200", got)
	}
}

func TestAnUnboundPanelIsClaimableByAnyInvitedUser(t *testing.T) {
	// A card prepared without a setup file still works. The hint is optional,
	// not a new way to brick an install.
	h, _ := newHandler()
	code, _ := submit(t, h)
	if got := claim(h, code, "anybody").StatusCode; got != 200 {
		t.Errorf("got %d, want 200", got)
	}
}

func TestCodesAreAcceptedAsPeopleTypeThem(t *testing.T) {
	h, _ := newHandler()
	code, _ := submit(t, h)
	typed := strings.ToLower(code[:4] + "-" + code[4:])
	if got := claim(h, typed, "owner-1").StatusCode; got != 200 {
		t.Errorf("claim with %q returned %d", typed, got)
	}
}

// TestClaimingAnExpiredCodeIs404 is spec section 11's "claiming an expired
// row": the fifteen-minute code bound must be enforced at claim time, not
// left to DynamoDB's TTL, which AWS documents as deleting expired items
// "within a few days" and which returns an expired-but-undeleted item in
// full from GetItem.
func TestClaimingAnExpiredCodeIs404(t *testing.T) {
	h, issuer := newHandler()
	h.CodeTTL = -1 * time.Minute // expired the instant it was minted
	code, _ := submit(t, h)
	if got := claim(h, code, "owner-1").StatusCode; got != 404 {
		t.Errorf("claim with an expired code returned %d, want 404", got)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("an expired code minted a certificate")
	}
}

// TestCollectingAnExpiredEnrollmentIs404 is spec section 11's other half: the
// 24-hour enrollment bound, enforced the same way and for the same reason.
func TestCollectingAnExpiredEnrollmentIs404(t *testing.T) {
	h, _ := newHandler()
	h.TTL = -1 * time.Minute
	_, token := submit(t, h)
	if got := collect(h, token).StatusCode; got != 404 {
		t.Errorf("collect of an expired enrollment returned %d, want 404", got)
	}
}

// TestClaimingAnExpiredEnrollmentIs404 covers the other half of the claim
// guard, which a mutation showed no test reached: with `now >= p.ExpiresAt`
// replaced by `now < 0` the suite stayed green. That half is not redundant
// with the code bound. RotateCode refreshes CodeExpiresAt on every poll and
// never touches ExpiresAt, so a panel polling for twenty-five hours holds a
// perfectly fresh code on a row that aged out -- and this check is the only
// thing that refuses it.
func TestClaimingAnExpiredEnrollmentIs404(t *testing.T) {
	h, issuer := newHandler()
	h.TTL = -1 * time.Minute // the row is already past its 24-hour bound
	code, _ := submit(t, h)  // ... while the code itself is freshly minted
	if got := claim(h, code, "owner-1").StatusCode; got != 404 {
		t.Errorf("claim against an expired enrollment returned %d, want 404", got)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("an expired enrollment minted a certificate")
	}
}

// TestAnUnverifiedEmailCannotSatisfyTheOwnerHint is the attack the review
// found: email is the only claim ownerMatches can ever be satisfied by (sub
// is a UUID nobody could type into a setup file), and email is writable by
// the signed-in caller themselves unless something stops it. Without the
// email_verified check, an invited user who read a pre-bound code off a
// screen could set their own email to the owner's address and claim it.
func TestAnUnverifiedEmailCannotSatisfyTheOwnerHint(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submitFor(t, h, "owner-1@example.com")

	for name, claims := range map[string]map[string]string{
		"email_verified false":  {"sub": "owner-2", "email": "owner-1@example.com", "email_verified": "false"},
		"email_verified absent": {"sub": "owner-3", "email": "owner-1@example.com"},
	} {
		if got := claimAs(h, code, claims).StatusCode; got != 404 {
			t.Errorf("%s: claim returned %d, want 404", name, got)
		}
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("an unverified email claim minted a certificate")
	}
	// The one-time guard was not consumed by the failed attempts: the real,
	// verified owner still succeeds.
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the verified owner's claim returned %d, want 200", got)
	}
}

// TestPollingBeforeExpiryDoesNotRotate is rotation coverage's first row: a
// poll that lands before the code expires must not hand back a new one, and
// the original code must still work.
func TestPollingBeforeExpiryDoesNotRotate(t *testing.T) {
	h, _ := newHandler()
	code, token := submit(t, h)
	resp := collect(h, token)
	if resp.StatusCode != 202 {
		t.Fatalf("collect returned %d", resp.StatusCode)
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(resp.Body), &out); err != nil {
		t.Fatal(err)
	}
	if _, present := out["code"]; present {
		t.Errorf("collect rotated a code that had not expired: %+v", out)
	}
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the un-rotated code failed to claim: %d", got)
	}
}

// TestPollingAfterExpiryRotatesAndRetiresTheOldCode closes the rest of
// rotation coverage's blind spot: a poll after expiry must hand back a
// different code, the old code must never claim again -- not even via the
// reservation it left behind (see Dynamo.ByCodeHash) -- and the new code must
// work. The mutations the review ran against this branch (deleting the
// rotation branch, inverting its condition, zeroing both TTLs) all left the
// suite green before this test existed.
func TestPollingAfterExpiryRotatesAndRetiresTheOldCode(t *testing.T) {
	h, issuer := newHandler()
	h.CodeTTL = -1 * time.Minute // expired before the first poll
	oldCode, token := submit(t, h)
	h.CodeTTL = 15 * time.Minute // restore, so the rotated code is not itself expired

	resp := collect(h, token)
	if resp.StatusCode != 202 {
		t.Fatalf("collect returned %d: %s", resp.StatusCode, resp.Body)
	}
	var out struct {
		Code          string `json:"code"`
		CodeExpiresAt int64  `json:"codeExpiresAt"`
	}
	if err := json.Unmarshal([]byte(resp.Body), &out); err != nil {
		t.Fatal(err)
	}
	if out.Code == "" {
		t.Fatal("a poll after expiry did not rotate the code")
	}
	if out.Code == oldCode {
		t.Fatal("rotation returned the same code")
	}
	if want := time.Now().Add(h.CodeTTL).Unix(); out.CodeExpiresAt < want-1 || out.CodeExpiresAt > want+1 {
		t.Errorf("codeExpiresAt = %d, want within a second of %d", out.CodeExpiresAt, want)
	}

	if got := claim(h, oldCode, "owner-1").StatusCode; got != 404 {
		t.Errorf("claiming the rotated-away code returned %d, want 404", got)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("a rotated-away code minted a certificate")
	}
	if got := claim(h, out.Code, "owner-1").StatusCode; got != 200 {
		t.Errorf("claiming the fresh code returned %d, want 200", got)
	}
}

// TestRotationDoesNotChangeTheCollectionToken: rotation is entirely a code
// concern. The device authenticates its poll with the collection token, so if
// rotation ever touched it, a panel would lose the ability to collect its own
// certificate.
func TestRotationDoesNotChangeTheCollectionToken(t *testing.T) {
	h, _ := newHandler()
	h.CodeTTL = -1 * time.Minute
	_, token := submit(t, h)
	h.CodeTTL = 15 * time.Minute

	if resp := collect(h, token); resp.StatusCode != 202 {
		t.Fatalf("collect that triggers rotation returned %d", resp.StatusCode)
	}
	// The same original token still resolves after rotation -- the only thing
	// that changed is the code.
	if resp := collect(h, token); resp.StatusCode != 202 {
		t.Errorf("collect with the original token returned %d after rotation, want 202", resp.StatusCode)
	}
}

// collidingStore fails a fixed number of Create calls with ErrCodeTaken
// before delegating to the wrapped Store. It exists to exercise submit's
// retry path without waiting on an actual 30^8 collision.
type collidingStore struct {
	enroll.Store
	remaining int
	calls     int
}

func (c *collidingStore) Create(ctx context.Context, p enroll.Pending) error {
	c.calls++
	if c.remaining > 0 {
		c.remaining--
		return enroll.ErrCodeTaken
	}
	return c.Store.Create(ctx, p)
}

func TestACodeCollisionIsRetriedNotSurfaced(t *testing.T) {
	// A duplicate code is a one-in-a-trillion event the caller can do nothing
	// about; submit must retry it the same way rotate does, not hand the
	// device a 409 on first boot.
	store := &collidingStore{Store: enroll.NewFake(), remaining: 2}
	h := &Handler{
		Enrollments: store,
		Devices:     devices.NewFake(),
		Issuer:      &enroll.FakeIssuer{CertPEM: "CERTPEM"},
		IoTEndpoint: "a1.iot.us-east-1.amazonaws.com",
		TTL:         24 * time.Hour,
		CodeTTL:     15 * time.Minute,
	}
	payload := `{"csr":` + strconv.Quote(csrPEM(t)) + `}`
	resp, err := h.Handle(context.Background(), post("/api/enroll", payload))
	if err != nil || resp.StatusCode != 201 {
		t.Fatalf("submit: status %d err %v body %s", resp.StatusCode, err, resp.Body)
	}
	if store.calls != 3 {
		t.Errorf("Create was called %d times, want 3 (2 collisions then a success)", store.calls)
	}
}

func TestACodeCollisionThatNeverResolvesIs500(t *testing.T) {
	// The 500 is reserved for a genuine failure: retries exhausted, not a
	// single collision.
	store := &collidingStore{Store: enroll.NewFake(), remaining: 100}
	h := &Handler{
		Enrollments: store,
		Devices:     devices.NewFake(),
		Issuer:      &enroll.FakeIssuer{CertPEM: "CERTPEM"},
		IoTEndpoint: "a1.iot.us-east-1.amazonaws.com",
		TTL:         24 * time.Hour,
		CodeTTL:     15 * time.Minute,
	}
	payload := `{"csr":` + strconv.Quote(csrPEM(t)) + `}`
	resp, _ := h.Handle(context.Background(), post("/api/enroll", payload))
	if resp.StatusCode != 500 {
		t.Errorf("got %d, want 500", resp.StatusCode)
	}
}
