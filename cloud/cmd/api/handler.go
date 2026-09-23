package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/accounts"
	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/idtoken"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/panelconfig"
	"hockeytrack-scoreboard/internal/reduce"
	"hockeytrack-scoreboard/internal/schedule"
	"hockeytrack-scoreboard/internal/season"
	"hockeytrack-scoreboard/internal/settings"
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
	// Accounts holds each account's default settings. Nil means nobody has
	// any, and the route that would save them says so.
	Accounts accounts.Store
	// Season is the NHL schedule, checked and cached (internal/season). Nil
	// means the picker has nothing to show and a schedule cannot be saved.
	Season func(ctx context.Context) (season.Season, error)
	// Now is the clock stamped onto a config message as chosenAt. Injected
	// so a test can move it; nil means time.Now.
	Now func() time.Time
	// Direct asks the director (cmd/director) to run for one panel, after
	// its schedule is saved, so the change is felt now rather than within
	// the minute. The API never works out or publishes a scheduled game
	// itself: that stays one principal's job. Nil means the minute sweep is
	// the only trigger.
	Direct func(ctx context.Context, thing string) error
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
	// Display is what was set on the panel, what it runs on once the
	// account's defaults are laid under that, and which layer each value
	// came from. Absent when the account's defaults could not be read: the
	// list then says less rather than something untrue.
	Display *settings.View `json:"display,omitempty"`
	// Wake is the owner's hand on the sleep switch while it is in force:
	// {"mode":"awake"|"asleep","until":ms}. Absent when the panel follows its
	// sleep hours.
	Wake *settings.Wake `json:"wake,omitempty"`
	// Schedule is what the owner asked this panel to show, and what the
	// rules make of it today.
	Schedule scheduleView `json:"schedule"`
}

// scheduleView is a panel's schedule as stored, plus what follows from it.
// Kept, Next and Undecided need the season; when it could not be read they
// are absent and Known is false, so the site can say so rather than show an
// empty list as if it were an answer.
type scheduleView struct {
	Games       []int64               `json:"games"`
	Templates   []string              `json:"templates"`
	Resolutions []schedule.Resolution `json:"resolutions"`
	Known       bool                  `json:"known"`
	Next        []season.Game         `json:"next,omitempty"`
	Undecided   [][]int64             `json:"undecided,omitempty"`
}

// nextShown is how many coming games a panel's row lists.
const nextShown = 3

func scheduleViewOf(p schedule.Panel, s *season.Season, now time.Time) scheduleView {
	v := scheduleView{Games: p.Games, Templates: p.Templates, Resolutions: p.Resolutions}
	if v.Games == nil {
		v.Games = []int64{}
	}
	if v.Templates == nil {
		v.Templates = []string{}
	}
	if v.Resolutions == nil {
		v.Resolutions = []schedule.Resolution{}
	}
	if s == nil {
		return v
	}
	v.Known = true
	out := schedule.Resolve(p, p.Games, nil, s.Starts(p.Games))
	v.Undecided = out.Undecided
	for _, id := range out.Kept {
		g, ok := s.Find(id)
		if !ok {
			continue
		}
		start, err := time.Parse(time.RFC3339, g.Start)
		if err != nil || !start.Add(schedule.Occupies).After(now) {
			continue
		}
		if v.Next = append(v.Next, g); len(v.Next) == nextShown {
			break
		}
	}
	return v
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
	// FinalAt is when the game ended, from the reducer. The site times a
	// final's hold from this; LastSeenAt is only the fallback for a game
	// that went final before the reducer recorded it.
	FinalAt int64 `json:"finalAt,omitempty"`
}

type teamView struct {
	Abbrev string `json:"abbrev"`
	Score  int    `json:"score"`
}

func viewOf(s reduce.State) *gameView {
	v := &gameView{State: s.GameState, Start: s.Start,
		Away: teamView{s.Away.Abbrev, s.Away.Score}, Home: teamView{s.Home.Abbrev, s.Home.Score},
		Intermission: s.Clock.Intermission, LastSeenAt: max(s.AsOf, s.SeenAt), FinalAt: s.FinalAt}
	v.Period.Label = s.Period.Label
	return v
}

// defaults reads the account's default settings. With no accounts store
// wired (an older deployment) an account simply has none.
func (h *Handler) defaults(ctx context.Context, sub string) (settings.Settings, error) {
	if h.Accounts == nil {
		return settings.Settings{}, nil
	}
	return h.Accounts.Defaults(ctx, sub)
}

// send publishes a panel's whole config document, retained. Every publish to
// a panel goes through here and through panelconfig.Compose: a retained
// message replaces the document, so one that carried only the game would
// wipe the settings from the panel's next reconnect, and one that carried
// only the settings would wipe the game.
func (h *Handler) send(ctx context.Context, d devices.Device, account settings.Settings) error {
	resolved, _ := settings.Resolve(account, d.Display)
	// A switch that has ended is not sent: the panel would ignore it, and the
	// document should say what is true.
	payload, err := panelconfig.Compose(d.GameID, d.ChosenAt, resolved, d.Wake.Live(h.now()))
	if err != nil {
		return err
	}
	return h.Pub.Publish(ctx, panelconfig.Topic(d.ThingName), payload, true)
}

// maxScheduleBody is the design's bound on a schedule request (section 8):
// 1,500 ten-digit ids and their answers fit with room to spare.
const maxScheduleBody = 64 << 10

// maxSettingsBody is far more than any settings document needs. Decode is
// strict about keys; this is strict about size before it parses anything.
const maxSettingsBody = 4 << 10

// seasonOrNil reads the season for a view. A schedule source that is down
// must not take the panels off the page; they are listed without what
// follows from it.
func (h *Handler) seasonOrNil(ctx context.Context) *season.Season {
	if h.Season == nil {
		return nil
	}
	s, err := h.Season(ctx)
	if err != nil {
		slog.Warn("season unavailable; listing panels without coming games", "err", err)
		return nil
	}
	return &s
}

// seasonOrNilFor is seasonOrNil for one panel, skipping the read when the
// panel has no schedule for it to explain.
func (h *Handler) seasonOrNilFor(ctx context.Context, d devices.Device) *season.Season {
	if d.Schedule.IsZero() {
		return nil
	}
	return h.seasonOrNil(ctx)
}

func (h *Handler) view(ctx context.Context, d devices.Device, account *settings.Settings, sn *season.Season) deviceView {
	v := deviceView{ThingName: d.ThingName, Name: d.Name, GameID: d.GameID, ChosenAt: d.ChosenAt,
		Wake:     d.Wake.Live(h.now()),
		Schedule: scheduleViewOf(d.Schedule, sn, h.now())}
	if account != nil {
		view := settings.ViewOf(*account, d.Display)
		v.Display = &view
	}
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
		// One read of the account's defaults for the whole list. If it
		// fails the panels are still listed, without resolved settings: the
		// list is the page, and it says less rather than something untrue.
		var account *settings.Settings
		if a, err := h.defaults(ctx, sub); err == nil {
			account = &a
		} else {
			slog.Warn("defaults lookup failed; listing panels without resolved settings", "err", err)
		}
		var sn *season.Season
		for _, d := range devs {
			if !d.Schedule.IsZero() { // nobody has a schedule: no need to fetch one
				sn = h.seasonOrNil(ctx)
				break
			}
		}
		out := make([]deviceView, 0, len(devs))
		for _, d := range devs {
			out = append(out, h.view(ctx, d, account, sn))
		}
		return respond(200, out)

	case "GET /api/settings":
		a, err := h.defaults(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		return respond(200, struct {
			Defaults settings.Settings `json:"defaults"`
			BuiltIn  settings.Wire     `json:"builtIn"`
		}{a, settings.BuiltIn.Wire()})

	case "PUT /api/settings":
		if h.Accounts == nil {
			return fail(500, "settings are not configured")
		}
		if len(rawBody) > maxSettingsBody {
			return fail(400, "invalid settings")
		}
		a, err := settings.Decode(rawBody)
		if err != nil {
			return fail(400, "invalid settings")
		}
		devs, err := h.Store.ListByOwner(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		// Saved first, unlike a single panel's change. This one fans out,
		// and a fan-out can half fail; with the defaults stored, saving
		// again -- or any later publish to a panel that was missed --
		// converges, because every publish is the whole document composed
		// from what is stored. The response says which panels were missed.
		if err := h.Accounts.SetDefaults(ctx, sub, a); err != nil {
			return fail(500, "save failed")
		}
		notSent := []string{}
		for _, d := range devs {
			if err := h.send(ctx, d, a); err != nil {
				slog.Warn("settings publish failed", "thing", d.ThingName, "err", err)
				notSent = append(notSent, d.ThingName)
			}
		}
		slog.Info("account defaults saved", "sub", sub, "panels", len(devs), "notSent", len(notSent))
		return respond(200, struct {
			Defaults settings.Settings `json:"defaults"`
			NotSent  []string          `json:"notSent"`
		}{a, notSent})

	case "PUT /api/devices/{thing}/display":
		if len(rawBody) > maxSettingsBody {
			return fail(400, "invalid settings")
		}
		overrides, err := settings.Decode(rawBody)
		if err != nil {
			return fail(400, "invalid settings")
		}
		d, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		// Without the account's defaults the document would carry built-in
		// values where the owner's belong. Nothing is sent that was built
		// on settings nobody could read.
		account, err := h.defaults(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		d.Display = overrides
		// Publish before persisting, for the reason the game route gives.
		if err := h.send(ctx, d, account); err != nil {
			return fail(502, "publish failed")
		}
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		slog.Info("panel settings saved", "sub", sub, "thing", d.ThingName)
		return respond(200, h.view(ctx, d, &account, h.seasonOrNilFor(ctx, d)))

	case "PUT /api/devices/{thing}/wake":
		// The owner's hand on the sleep switch: awake, asleep, or auto (follow
		// sleep hours). The body names a mode and nothing else. When it ends
		// is worked out here, from the panel's own sleep hours, and is never
		// taken from a client: a request cannot pin a panel lit or dark.
		if len(rawBody) > maxSettingsBody {
			return fail(400, "invalid mode")
		}
		var body struct {
			Mode string `json:"mode"`
		}
		dec := json.NewDecoder(bytes.NewReader(rawBody))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&body); err != nil || dec.More() {
			return fail(400, "invalid mode")
		}
		d, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		account, err := h.defaults(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		resolved, _ := settings.Resolve(account, d.Display)
		wake, err := settings.NewWake(body.Mode, h.now(), resolved.Sleep)
		if err != nil {
			return fail(400, "invalid mode")
		}
		// chosenAt is left alone: this is not a choice of game, and a panel
		// reads a new stamp as one.
		d.Wake = wake
		// Publish before persisting, for the reason the game route gives.
		if err := h.send(ctx, d, account); err != nil {
			return fail(502, "publish failed")
		}
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		slog.Info("panel wake switch set", "sub", sub, "thing", d.ThingName, "mode", body.Mode)
		return respond(200, h.view(ctx, d, &account, h.seasonOrNilFor(ctx, d)))

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
		// the owner presses the button -- and ONLY then, which is why it is
		// stored: a publish that is not a choice re-sends it unchanged.
		account, err := h.defaults(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		d.GameID, d.ChosenAt = body.GameID, h.now().UnixMilli()
		// Publish before persisting. If Update then fails, the panel is
		// already showing the new game while the stored record still shows
		// the old one. The reverse order trades that for a worse mismatch —
		// a record claiming a change the panel never received, when the
		// panel is the thing the owner is actually looking at. The retained
		// publish is idempotent, so a retry (or the next successful change)
		// converges the record either way.
		if err := h.send(ctx, d, account); err != nil {
			return fail(502, "publish failed")
		}
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		return respond(200, h.view(ctx, d, &account, h.seasonOrNilFor(ctx, d)))

	case "GET /api/schedule":
		// The whole season, for the picker. Public data, served here only
		// because the browser cannot fetch it from another origin without
		// widening this site's policy; nothing about the caller goes into it.
		if h.Season == nil {
			return fail(500, "no schedule source")
		}
		sn, err := h.Season(ctx)
		if err != nil {
			slog.Warn("season unavailable", "err", err)
			return fail(502, "schedule unavailable")
		}
		return respond(200, struct {
			Teams map[string]string `json:"teams"`
			Games []season.Game     `json:"games"`
		}{sn.Teams, sn.Games})

	case "PUT /api/devices/{thing}/schedule":
		// Size, then shape, then ownership, then the rules. Nothing is
		// published from here: this records what the owner asked for and
		// asks the director to act on it. A panel still follows gameId, and
		// the director is what sets it.
		if len(rawBody) > maxScheduleBody {
			return fail(400, "invalid schedule")
		}
		asked, err := schedule.Decode(rawBody)
		if err != nil {
			return fail(400, "invalid schedule")
		}
		d, ok, err := h.owned(ctx, thing, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		if !ok {
			return fail(404, "no such device")
		}
		if len(asked.Templates) > 0 {
			// A template is looked up under the caller, and the caller has
			// none: there is nowhere to make one yet. Not-yours and
			// not-there are the same answer on purpose.
			return fail(404, "no such template")
		}
		if h.Season == nil {
			return fail(500, "no schedule source")
		}
		sn, err := h.Season(ctx)
		if err != nil {
			// Without the season a game id cannot be vouched for, and an
			// overlap cannot be seen. Nothing is stored on a guess.
			slog.Warn("season unavailable; schedule not saved", "err", err)
			return fail(502, "schedule unavailable")
		}
		saved, err := schedule.Save(asked, d.Schedule, sn.Starts(asked.Games))
		switch {
		case errors.Is(err, schedule.ErrUnknownGame):
			return fail(400, "unknown game")
		case errors.Is(err, schedule.ErrBadResolution):
			return fail(400, "invalid resolution")
		case err != nil:
			return fail(400, "invalid schedule")
		}
		if len(saved.Unresolved) > 0 {
			// The owner is right here, so nothing is decided for them
			// (decision 8). The conflicts go back; nothing is stored.
			return respond(409, struct {
				Error      string    `json:"error"`
				Unresolved [][]int64 `json:"unresolved"`
			}{"unresolved conflicts", saved.Unresolved})
		}
		d.Schedule = saved.Panel
		if err := h.Store.Update(ctx, d); err != nil {
			return fail(500, "save failed")
		}
		slog.Info("panel schedule saved", "sub", sub, "thing", d.ThingName,
			"games", len(d.Schedule.Games), "resolutions", len(d.Schedule.Resolutions))
		if h.Direct != nil {
			// The save is done; a director that cannot be reached is a
			// change felt within the minute, not a failed save.
			if err := h.Direct(ctx, d.ThingName); err != nil {
				slog.Warn("director not asked; the next minute will act", "thing", d.ThingName, "err", err)
			}
		}
		return respond(200, scheduleViewOf(d.Schedule, &sn, h.now()))

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
		return respond(200, h.view(ctx, d, nil, h.seasonOrNilFor(ctx, d)))

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
