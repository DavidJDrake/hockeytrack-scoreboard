package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/iotpub"
)

// Handler serves the admin API. Every device-scoped route resolves the
// device by thing name, then checks the caller owns it; a caller who does
// not own it gets 404, so the API never confirms a thing exists.
type Handler struct {
	Store devices.Store
	Pub   iotpub.Publisher
	Games func(ctx context.Context) ([]byte, error)
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

func subject(req events.APIGatewayV2HTTPRequest) string {
	if req.RequestContext.Authorizer == nil || req.RequestContext.Authorizer.JWT == nil {
		return ""
	}
	return req.RequestContext.Authorizer.JWT.Claims["sub"]
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
	sub := subject(req)
	if sub == "" {
		return fail(401, "unauthenticated")
	}
	route := req.RequestContext.RouteKey
	if route == "" {
		route = req.RouteKey
	}
	thing := req.PathParameters["thing"]

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

	case "POST /api/devices/claim":
		var body struct {
			Code string `json:"code"`
		}
		if err := json.Unmarshal([]byte(req.Body), &body); err != nil || body.Code == "" {
			return fail(400, "a code is required")
		}
		d, found, err := h.Store.ByCode(ctx, body.Code)
		if err != nil {
			return fail(500, "claim failed")
		}
		if !found {
			return fail(404, "no unclaimed device with that code")
		}
		if err := h.Store.Claim(ctx, d.ThingName, sub); err != nil {
			if errors.Is(err, devices.ErrAlreadyClaimed) {
				return fail(404, "no unclaimed device with that code")
			}
			return fail(500, "claim failed")
		}
		return respond(200, deviceView{ThingName: d.ThingName})

	case "PUT /api/devices/{thing}/game":
		var body struct {
			GameID int64 `json:"gameId"`
		}
		if err := json.Unmarshal([]byte(req.Body), &body); err != nil || body.GameID == 0 {
			return fail(400, "a gameId is required")
		}
		d, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		payload, err := json.Marshal(map[string]int64{"gameId": body.GameID})
		if err != nil {
			return fail(500, "encode failed")
		}
		topic := fmt.Sprintf("scoreboard/%s/config", d.ThingName)
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
		if err := json.Unmarshal([]byte(req.Body), &body); err != nil || body.Name == "" {
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
		if _, ok, err := h.owned(ctx, thing, sub); err != nil || !ok {
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
