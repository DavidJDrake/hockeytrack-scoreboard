package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/idtoken"
	"hockeytrack-scoreboard/internal/iotpub"
)

// Handler serves the admin API. Every device-scoped route resolves the
// device by thing name, then checks the caller owns it; a caller who does
// not own it gets 404, so the API never confirms a thing exists.
type Handler struct {
	Store  devices.Store
	Pub    iotpub.Publisher
	Games  func(ctx context.Context) ([]byte, error)
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
			out = append(out, deviceView{ThingName: d.ThingName, Name: d.Name, GameID: d.GameID})
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
		payload, err := json.Marshal(struct {
			GameID   int64 `json:"gameId"`
			ChosenAt int64 `json:"chosenAt"`
		}{body.GameID, h.now().UnixMilli()})
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
		d.GameID = body.GameID
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		return respond(200, deviceView{ThingName: d.ThingName, Name: d.Name, GameID: d.GameID})

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
		return respond(200, deviceView{ThingName: d.ThingName, Name: d.Name, GameID: d.GameID})

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
