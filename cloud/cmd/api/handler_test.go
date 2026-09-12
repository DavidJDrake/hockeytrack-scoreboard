package main

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/iotpub"
)

func req(method, route, sub, body string, params map[string]string) events.APIGatewayV2HTTPRequest {
	r := events.APIGatewayV2HTTPRequest{Body: body, PathParameters: params}
	r.RequestContext.HTTP.Method = method
	r.RequestContext.RouteKey = route
	r.RouteKey = route
	if sub != "" {
		r.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
			JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{Claims: map[string]string{"sub": sub}},
		}
	}
	return r
}

func handlerWith(t *testing.T) (*Handler, *devices.Fake, *iotpub.Fake) {
	t.Helper()
	st, pub := devices.NewFake(), &iotpub.Fake{}
	_ = st.Register(context.Background(), "scoreboard-7qf2", "7QF2")
	return &Handler{Store: st, Pub: pub}, st, pub
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

func TestClaimBindsByCodeAndRefusesAUsedCode(t *testing.T) {
	h, _, _ := handlerWith(t)
	ctx := context.Background()

	res, err := h.Handle(ctx, req("POST", "POST /api/devices/claim", "sub-a", `{"code":"7QF2"}`, nil))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 200 {
		t.Fatalf("first claim status = %d, want 200 (body %s)", res.StatusCode, res.Body)
	}
	res, _ = h.Handle(ctx, req("POST", "POST /api/devices/claim", "sub-b", `{"code":"7QF2"}`, nil))
	if res.StatusCode != 404 {
		t.Errorf("second claim status = %d, want 404", res.StatusCode)
	}
}

func TestListReturnsOnlyTheCallersDevices(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Register(ctx, "scoreboard-aaaa", "AAAA")
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
