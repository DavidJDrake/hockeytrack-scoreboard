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

func claim(h *Handler, code, sub string) events.APIGatewayV2HTTPResponse {
	req := post("/api/devices/claim", `{"code":"`+code+`"}`)
	req.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{
			Claims: map[string]string{"sub": sub, "email": sub + "@example.com"},
		},
	}
	resp, _ := h.Handle(context.Background(), req)
	return resp
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
