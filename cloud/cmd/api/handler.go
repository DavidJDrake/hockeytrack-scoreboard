package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/idtoken"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/reduce"
)

// Handler serves the admin API. Every device-scoped route resolves the
// device by thing name, then checks the caller owns it; a caller who does
// not own it gets 404, so the API never confirms a thing exists.
type Handler struct {
	Store devices.Store
	Pub   iotpub.Publisher
	Games func(ctx context.Context) ([]byte, error)
	// Game reads one game's state, read-only, from the table the reducer
	// keeps. Nil means the list goes without.
	Game   func(ctx context.Context, gameID int64) (reduce.State, bool, error)
	Tokens idtoken.Tokens
	// Now is the clock stamped onto a config message as chosenAt. Injected
	// so a test can move it; nil means time.Now.
	Now func() time.Time
}

func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

type deviceView struct {
	ThingName string `json:"thingName"`
	Name      string `json:"name"`
	GameID    int64  `json:"gameId"`
	// ChosenAt and Game are what the site needs to run the panel's own rule
	// and say what the panel should be showing. Game is absent when the
	// panel follows nothing, when the reducer has not seen the game yet, or
	// when the lookup failed: the site says less, it does not say nothing.
	ChosenAt int64     `json:"chosenAt,omitempty"`
	Game     *gameView `json:"game,omitempty"`
}

// gameView is the little of a reducer State the site's sentence is made of.
// Named fields, copied one by one: a State also carries rosters and penalty
// bookkeeping, and none of that has any business in an owner's panel list.
type gameView struct {
	State  string   `json:"state"`
	Start  string   `json:"start,omitempty"`
	Away   teamView `json:"away"`
	Home   teamView `json:"home"`
	Period struct {
		Label string `json:"label"`
	} `json:"period"`
	Intermission bool `json:"intermission,omitempty"`
	// LastSeenAt is the later of the reducer's clock anchor and its last
	// heartbeat, in milliseconds. Heartbeats stop when a game ends, so for a
	// final this is about when it ended.
	LastSeenAt int64 `json:"lastSeenAt,omitempty"`
}

type teamView struct {
	Abbrev string `json:"abbrev"`
	Score  int    `json:"score"`
}

func viewOf(s reduce.State) *gameView {
	v := &gameView{State: s.GameState, Start: s.Start,
		Away: teamView{s.Away.Abbrev, s.Away.Score}, Home: teamView{s.Home.Abbrev, s.Home.Score},
		Intermission: s.Clock.Intermission, LastSeenAt: max(s.AsOf, s.SeenAt)}
	v.Period.Label = s.Period.Label
	return v
}

func (h *Handler) view(ctx context.Context, d devices.Device) deviceView {
	v := deviceView{ThingName: d.ThingName, Name: d.Name, GameID: d.GameID, ChosenAt: d.ChosenAt}
	if h.Game == nil || d.GameID == 0 {
		return v
	}
	s, found, err := h.Game(ctx, d.GameID)
	if err != nil {
		// The list is the page. A games table that is down must not take
		// the panels off it.
		slog.Warn("game lookup failed; listing the panel without it", "gameId", d.GameID, "err", err)
		return v
	}
	if found {
		v.Game = viewOf(s)
	}
	return v
}

func respond(status int, body any) (events.APIGatewayV2HTTPResponse, error) {
	b, err := json.Marshal(body)
	if err != nil {
		return events.APIGatewayV2HTTPResponse{StatusCode: 500}, nil
	}
	return events.APIGatewayV2HTTPResponse{
		StatusCode: status,
		Headers:    map[string]string{"content-type": "application/json"},
		Body:       string(b),
	}, nil
}

func fail(status int, msg string) (events.APIGatewayV2HTTPResponse, error) {
	return respond(status, map[string]string{"error": msg})
}

// decodeBody returns the request body, base64-decoding it first if API
// Gateway marked it as encoded. Unmarshalling req.Body directly would fail
// silently in that case, surfacing as a confusing 400 rather than the real
// cause.
func decodeBody(req events.APIGatewayV2HTTPRequest) ([]byte, error) {
	if !req.IsBase64Encoded {
		return []byte(req.Body), nil
	}
	return base64.StdEncoding.DecodeString(req.Body)
}

// owned returns the device if the caller owns it, or false. Callers must
// treat false as 404.
func (h *Handler) owned(ctx context.Context, thing, sub string) (devices.Device, bool, error) {
	d, found, err := h.Store.Get(ctx, thing)
	if err != nil || !found || d.Owner != sub {
		return devices.Device{}, false, err
	}
	return d, true, nil
}

// Handle routes one request. The route key is API Gateway's, e.g.
// "PUT /api/devices/{thing}/game".
func (h *Handler) Handle(ctx context.Context, req events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
	caller, err := idtoken.Authenticate(ctx, h.Tokens, req)
	if errors.Is(err, idtoken.ErrUnavailable) {
		return fail(503, "sign-in check unavailable")
	}
	if err != nil {
		return fail(401, "unauthenticated")
	}
	sub := caller.Sub
	route := req.RequestContext.RouteKey
	if route == "" {
		route = req.RouteKey
	}
	thing := req.PathParameters["thing"]
	rawBody, err := decodeBody(req)
	if err != nil {
		return fail(400, "invalid request body")
	}

	switch route {
	case "GET /api/devices":
		devs, err := h.Store.ListByOwner(ctx, sub)
		if err != nil {
			return fail(500, "list failed")
		}
		out := make([]deviceView, 0, len(devs))
		for _, d := range devs {
			out = append(out, h.view(ctx, d))
		}
		return respond(200, out)

	case "PUT /api/devices/{thing}/game":
		var body struct {
			GameID int64 `json:"gameId"`
		}
		if err := json.Unmarshal(rawBody, &body); err != nil || body.GameID == 0 {
			return fail(400, "a gameId is required")
		}
		d, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		// gameId stays a top-level number: panels in the field (v0.1.3)
		// read only that key and ignore the rest, so adding to this
		// document must never move it.
		//
		// chosenAt is when the owner pressed the button, on the server's
		// clock. It exists because the panel cannot otherwise tell a press
		// from a repetition: this topic is retained, and a panel that was
		// offline when the button was pressed is handed the same payload on
		// reconnect, flagged as a replay -- which it ignores, correctly,
		// because that is how it survives reconnecting every hour. With a
		// stamp, a replay carrying a chosenAt it has not acted on is news,
		// and the press that happened during the outage is not lost. Panels
		// compare these values only against each other, never against their
		// own clocks, which on a board with no RTC may be anything at all --
		// and they compare them for DIFFERENCE, not for order, so this
		// value need not be monotonic across deployments, execution
		// environments or a region failover. It only has to change when
		// the owner presses the button.
		// One stamp, for the panel and for the record: the site runs the
		// panel's rule from the stored one, so they must be the same moment.
		chosenAt := h.now().UnixMilli()
		payload, err := json.Marshal(struct {
			GameID   int64 `json:"gameId"`
			ChosenAt int64 `json:"chosenAt"`
		}{body.GameID, chosenAt})
		if err != nil {
			return fail(500, "encode failed")
		}
		topic := fmt.Sprintf("scoreboard/%s/config", d.ThingName)
		// Publish before persisting. If Update then fails, the panel is
		// already showing the new game while the stored record still shows
		// the old one. The reverse order trades that for a worse mismatch —
		// a record claiming a change the panel never received, when the
		// panel is the thing the owner is actually looking at. The retained
		// publish is idempotent, so a retry (or the next successful change)
		// converges the record either way.
		if err := h.Pub.Publish(ctx, topic, payload, true); err != nil {
			return fail(502, "publish failed")
		}
		d.GameID, d.ChosenAt = body.GameID, chosenAt
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		return respond(200, h.view(ctx, d))

	case "PATCH /api/devices/{thing}":
		var body struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(rawBody, &body); err != nil || body.Name == "" {
			return fail(400, "a name is required")
		}
		d, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		d.Name = body.Name
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		return respond(200, h.view(ctx, d))

	case "DELETE /api/devices/{thing}":
		_, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		if err := h.Store.Unbind(ctx, thing, sub); err != nil {
			return fail(500, "unbind failed")
		}
		return respond(200, map[string]string{"thingName": thing})

	case "GET /api/games":
		if h.Games == nil {
			return fail(500, "no game source")
		}
		body, err := h.Games(ctx)
		if err != nil {
			return fail(502, "schedule unavailable")
		}
		return events.APIGatewayV2HTTPResponse{
			StatusCode: 200,
			Headers:    map[string]string{"content-type": "application/json"},
			Body:       string(body),
		}, nil
	}
	return fail(404, "no such route")
}
