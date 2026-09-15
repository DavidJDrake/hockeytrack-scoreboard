package main

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/idtoken"
	"hockeytrack-scoreboard/internal/idtoken/idtokentest"
	"hockeytrack-scoreboard/internal/iotpub"
)

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

func TestSettingAGamePublishesRetainedConfigToThatDevicesTopic(t *testing.T) {
	h, st, pub := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")

	res, err := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a",
		`{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"}))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 200 {
		t.Fatalf("status = %d, want 200 (body %s)", res.StatusCode, res.Body)
	}
	if len(pub.Messages) != 1 {
		t.Fatalf("published %d messages, want 1", len(pub.Messages))
	}
	m := pub.Messages[0]
	if m.Topic != "scoreboard/scoreboard-7qf2/config" {
		t.Errorf("topic = %q, want scoreboard/scoreboard-7qf2/config", m.Topic)
	}
	if !m.Retain {
		t.Error("config was published without retain; a panel that reconnects would not get it")
	}
	var payload struct {
		GameID int64 `json:"gameId"`
	}
	if err := json.Unmarshal(m.Payload, &payload); err != nil || payload.GameID != 2026020001 {
		t.Errorf("payload = %s, want {\"gameId\":2026020001}", m.Payload)
	}
}

func TestAStrangerGets404AndNoPublish(t *testing.T) {
	h, st, pub := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")

	res, err := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-b",
		`{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"}))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 404 {
		t.Errorf("status = %d, want 404 — 403 would confirm the device exists", res.StatusCode)
	}
	if len(pub.Messages) != 0 {
		t.Errorf("published %d messages for a non-owner, want 0", len(pub.Messages))
	}
}

func TestListReturnsOnlyTheCallersDevices(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Register(ctx, "scoreboard-aaaa")
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	_ = st.Claim(ctx, "scoreboard-aaaa", "sub-b")

	res, err := h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(res.Body, "scoreboard-aaaa") {
		t.Errorf("listing leaked another owner's device: %s", res.Body)
	}
	if !strings.Contains(res.Body, "scoreboard-7qf2") {
		t.Errorf("listing omitted the caller's own device: %s", res.Body)
	}
}

func TestAStrangerCannotRenameAnotherOwnersDevice(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")

	res, err := h.Handle(ctx, req("PATCH", "PATCH /api/devices/{thing}", "sub-b",
		`{"name":"Not yours"}`, map[string]string{"thing": "scoreboard-7qf2"}))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 404 {
		t.Errorf("status = %d, want 404 — 403 would confirm the device exists", res.StatusCode)
	}
	d, _, _ := st.Get(ctx, "scoreboard-7qf2")
	if d.Name != "" {
		t.Errorf("stranger's rename mutated the device: name = %q", d.Name)
	}
}

func TestAStrangerCannotUnbindAnotherOwnersDevice(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")

	res, err := h.Handle(ctx, req("DELETE", "DELETE /api/devices/{thing}", "sub-b",
		"", map[string]string{"thing": "scoreboard-7qf2"}))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 404 {
		t.Errorf("status = %d, want 404 — 403 would confirm the device exists", res.StatusCode)
	}
	d, found, _ := st.Get(ctx, "scoreboard-7qf2")
	if !found || d.Owner != "sub-a" {
		t.Errorf("stranger's delete changed ownership: found=%v owner=%q, want sub-a", found, d.Owner)
	}
}

func TestAnUnauthenticatedRequestIsRejectedWithoutTouchingTheStore(t *testing.T) {
	h, _, pub := handlerWith(t)
	res, err := h.Handle(context.Background(), req("GET", "GET /api/devices", "", "", nil))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 401 {
		t.Errorf("status = %d, want 401", res.StatusCode)
	}
	if len(pub.Messages) != 0 {
		t.Error("an unauthenticated request caused a publish")
	}
}

func TestForgedAuthorizerClaimsWithoutATokenAreRefusedAndLogged(t *testing.T) {
	// A hand-built event sent straight to the function, for every route this
	// API serves: the owner's sub in the authorizer block, and no token.
	// terraform/admin.tf local.admin_routes is the source of truth for the
	// route list.
	cases := []struct {
		method string
		route  string
		body   string
		params map[string]string
	}{
		{"GET", "GET /api/devices", "", nil},
		{"PUT", "PUT /api/devices/{thing}/game", `{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"}},
		{"PATCH", "PATCH /api/devices/{thing}", `{"name":"Not yours"}`, map[string]string{"thing": "scoreboard-7qf2"}},
		{"DELETE", "DELETE /api/devices/{thing}", "", map[string]string{"thing": "scoreboard-7qf2"}},
		{"GET", "GET /api/games", "", nil},
	}
	for _, tc := range cases {
		t.Run(tc.route, func(t *testing.T) {
			h, st, pub := handlerWith(t)
			ctx := context.Background()
			_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
			var buf bytes.Buffer
			prev := slog.Default()
			slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
			t.Cleanup(func() { slog.SetDefault(prev) })

			r := req(tc.method, tc.route, "", tc.body, tc.params)
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
			d, found, _ := st.Get(ctx, "scoreboard-7qf2")
			if !found || d.Owner != "sub-a" || d.Name != "" {
				t.Errorf("forged event changed store state: found=%v owner=%q name=%q", found, d.Owner, d.Name)
			}
			if !strings.Contains(buf.String(), idtoken.MismatchMessage) {
				t.Errorf("no mismatch line logged: %s", buf.String())
			}
		})
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
