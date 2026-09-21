package main

import (
	"errors"
	"hockeytrack-scoreboard/internal/reduce"

	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"

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

// A clock the tests move by hand, so "chosenAt changed" is a fact about the
// handler rather than about how fast the test ran.
var clock = time.Date(2026, 10, 1, 18, 0, 0, 0, time.UTC)

func handlerWith(t *testing.T) (*Handler, *devices.Fake, *iotpub.Fake) {
	t.Helper()
	st, pub := devices.NewFake(), &iotpub.Fake{}
	_ = st.Register(context.Background(), "scoreboard-7qf2")
	return &Handler{Store: st, Pub: pub, Tokens: idtokentest.Fake{},
		Now: func() time.Time { return clock }}, st, pub
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
		GameID   int64 `json:"gameId"`
		ChosenAt int64 `json:"chosenAt"`
	}
	if err := json.Unmarshal(m.Payload, &payload); err != nil || payload.GameID != 2026020001 {
		t.Errorf("payload = %s, want gameId 2026020001", m.Payload)
	}
	// chosenAt is what lets a panel tell one press of "Show on panel" from
	// the broker replaying the same retained message on every reconnect.
	// Without it, a press made while the panel was offline is delivered as
	// a replay of a message it has already ignored, and is lost -- while
	// the site says it worked. The panel compares chosenAt values only
	// with each other, so it has to be the server's clock, not the panel's.
	if payload.ChosenAt != h.Now().UnixMilli() {
		t.Errorf("chosenAt = %d, want the server clock %d", payload.ChosenAt, h.Now().UnixMilli())
	}
}

func TestSettingTheSameGameTwicePublishesTwoDifferentChosenAts(t *testing.T) {
	// The press that has to survive: an owner presses "Show on panel"
	// while the panel is offline. Nothing about the message changes except
	// this stamp, and that is the whole difference between "the owner asked
	// for this again" and "the broker is repeating itself".
	h, st, pub := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")

	stamps := make([]int64, 0, 2)
	for i := 0; i < 2; i++ {
		clock = clock.Add(time.Second)
		if _, err := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a",
			`{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"})); err != nil {
			t.Fatal(err)
		}
		var payload struct {
			ChosenAt int64 `json:"chosenAt"`
		}
		if err := json.Unmarshal(pub.Messages[i].Payload, &payload); err != nil {
			t.Fatal(err)
		}
		stamps = append(stamps, payload.ChosenAt)
	}
	if stamps[1] <= stamps[0] {
		t.Errorf("chosenAt did not move: %d then %d", stamps[0], stamps[1])
	}
}

func TestTheConfigPayloadStillLeadsWithGameId(t *testing.T) {
	// v0.1.3 panels are in the field and read gameId only. An unknown key
	// has to be something they ignore, which JSON decoding into a struct
	// does -- this pins the shape so a future change cannot quietly turn
	// gameId into a nested field and strand them.
	h, st, pub := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	if _, err := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a",
		`{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"})); err != nil {
		t.Fatal(err)
	}
	var flat map[string]any
	if err := json.Unmarshal(pub.Messages[0].Payload, &flat); err != nil {
		t.Fatal(err)
	}
	if _, ok := flat["gameId"].(float64); !ok {
		t.Errorf("gameId is not a top-level number in %s", pub.Messages[0].Payload)
	}
	// Exactly these three. "display" arrived with settings from the site
	// (2026-09-20); v0.1.3 to v0.1.5 ignore it, which device/tests checks
	// against the documents in testdata/config-documents.json. Anything
	// else appearing here is a change somebody should have to defend.
	if _, ok := flat["display"].(map[string]any); !ok || len(flat) != 3 || flat["chosenAt"] == nil {
		t.Errorf("payload keys want gameId, chosenAt, display: %s", pub.Messages[0].Payload)
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

// The home page says what a panel should be showing. It can only say that if
// the list carries what the panel's own rule needs: when the owner last
// chose, and the game's state.
func TestListCarriesWhenTheGameWasChosenAndWhatStateItIsIn(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	h.Game = func(_ context.Context, id int64) (reduce.State, bool, error) {
		if id != 2026020001 {
			return reduce.State{}, false, nil
		}
		return reduce.State{GameID: id, GameState: "FINAL", Start: "2026-10-01T23:00:00Z", AsOf: 1000, SeenAt: 2000,
			Away: reduce.Team{Abbrev: "MTL", Score: 1}, Home: reduce.Team{Abbrev: "TOR", Score: 4},
			Period: reduce.Period{Number: 3, Label: "3"}}, true, nil
	}
	if res, _ := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`,
		map[string]string{"thing": "scoreboard-7qf2"})); res.StatusCode != 200 {
		t.Fatalf("set game: %d", res.StatusCode)
	}
	res, _ := h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	var got []struct {
		ChosenAt int64 `json:"chosenAt"`
		Game     *struct {
			State      string `json:"state"`
			Start      string `json:"start"`
			LastSeenAt int64  `json:"lastSeenAt"`
			Away       struct {
				Abbrev string `json:"abbrev"`
				Score  int    `json:"score"`
			} `json:"away"`
			Period struct {
				Label string `json:"label"`
			} `json:"period"`
		} `json:"game"`
	}
	if err := json.Unmarshal([]byte(res.Body), &got); err != nil || len(got) != 1 {
		t.Fatalf("body %s: %v", res.Body, err)
	}
	if got[0].ChosenAt != clock.UnixMilli() {
		t.Errorf("chosenAt = %d, want the moment the game was set (%d)", got[0].ChosenAt, clock.UnixMilli())
	}
	g := got[0].Game
	if g == nil || g.State != "FINAL" || g.Start != "2026-10-01T23:00:00Z" || g.Away.Abbrev != "MTL" || g.Away.Score != 1 || g.Period.Label != "3" {
		t.Fatalf("game = %+v", g)
	}
	if g.LastSeenAt != 2000 {
		t.Errorf("lastSeenAt = %d, want the later of asOf and seenAt (2000)", g.LastSeenAt)
	}
	// Nothing the reducer keeps for itself leaks: rosters, penalties' bookkeeping.
	if strings.Contains(res.Body, "roster") || strings.Contains(res.Body, "penalt") {
		t.Errorf("list leaks reducer internals: %s", res.Body)
	}
}

func TestListSaysNothingAboutAGameItCannotFind(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	lookups := 0
	h.Game = func(context.Context, int64) (reduce.State, bool, error) { lookups++; return reduce.State{}, false, nil }

	res, _ := h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	if lookups != 0 {
		t.Errorf("looked up a game for a panel following none")
	}
	if strings.Contains(res.Body, `"game"`) {
		t.Errorf("a panel following nothing has a game: %s", res.Body)
	}

	// A game the reducer has not seen yet (it has not started): the list
	// still answers, without one. The site falls back on today's schedule.
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020009}`, map[string]string{"thing": "scoreboard-7qf2"}))
	res, _ = h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	if res.StatusCode != 200 || strings.Contains(res.Body, `"game"`) {
		t.Errorf("status %d body %s", res.StatusCode, res.Body)
	}
}

// The list is the page. A games table that is down must not take the panels
// off it; the site says less, it does not say nothing.
func TestListSurvivesTheGamesTableBeingDown(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"}))
	h.Game = func(context.Context, int64) (reduce.State, bool, error) {
		return reduce.State{}, false, errors.New("throttled")
	}
	res, _ := h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	if res.StatusCode != 200 || !strings.Contains(res.Body, "scoreboard-7qf2") || strings.Contains(res.Body, `"game"`) {
		t.Errorf("status %d body %s", res.StatusCode, res.Body)
	}
}

func TestReleasingAPanelForgetsWhenItsGameWasChosen(t *testing.T) {
	h, st, _ := handlerWith(t)
	ctx := context.Background()
	_ = st.Claim(ctx, "scoreboard-7qf2", "sub-a")
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, map[string]string{"thing": "scoreboard-7qf2"}))
	_ = st.Unbind(ctx, "scoreboard-7qf2", "sub-a")
	d, _, _ := st.Get(ctx, "scoreboard-7qf2")
	if d.ChosenAt != 0 || d.GameID != 0 {
		t.Errorf("the next owner inherits %+v", d)
	}
}
