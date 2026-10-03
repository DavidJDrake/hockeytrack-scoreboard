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
	"hockeytrack-scoreboard/internal/templates"
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
	// Templates holds each account's templates (internal/templates), read
	// under the caller's subject and never by id alone. Nil means nobody has
	// any: the template routes say so, a schedule naming one is refused as
	// not found, and a row that names one anyway is listed with Known false,
	// as it is when the table is down, never with a kept set built without
	// the template games in it.
	Templates templates.Store
	// NewID makes a template id. Injected so a test can make a collision;
	// nil means templates.NewID.
	NewID func() string
}

func (h *Handler) newID() string {
	if h.NewID != nil {
		return h.NewID()
	}
	return templates.NewID()
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
// Kept, Next and Undecided need the season and, for a panel with templates,
// the templates' games; when either could not be read they are absent and
// Known is false, so the site can say so rather than show an empty list, or
// a list missing the template games, as if it were an answer.
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

// outcome is what the rules make of a panel's schedule today: its own games
// and its templates' games together, the panel's own winning any unanswered
// conflict. tpls is the owner's templates by id (templates.GamesOf); a panel
// with none needs none. Every kept set shown or acted on comes from here, so
// the site, the director and a template edit's report agree.
func outcome(p schedule.Panel, tpls map[string][]int64, s *season.Season) schedule.Outcome {
	games, sources := schedule.Candidates(p, tpls)
	return schedule.Resolve(p, games, sources, s.Starts(games))
}

func scheduleViewOf(p schedule.Panel, tpls map[string][]int64, s *season.Season, now time.Time) scheduleView {
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
	out := outcome(p, tpls, s)
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
	//
	// The next game is nil here, deliberately. Working out a panel's next
	// kept game is the director's rule (internal/director.Next) over a
	// situation only the director builds, and the API never works out a
	// scheduled game itself: that stays one principal's job. So a publish
	// from here -- a choice, a settings save, the sleep switch -- carries no
	// next, and the panel's strip is empty until the director's next publish
	// to that panel fills it, which is its next change of game. What is not
	// covered: the director publishes only on a change of game, so a settings
	// save between two games leaves the strip empty until the second starts.
	payload, err := panelconfig.Compose(d.GameID, d.ChosenAt, resolved, d.Wake.Live(h.now()), nil)
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

// viewOne is view for a route that answers with one panel, reading what its
// schedule needs.
func (h *Handler) viewOne(ctx context.Context, d devices.Device, account *settings.Settings) deviceView {
	sn, tpls := h.scheduleInputs(ctx, d.Owner, []devices.Device{d})
	return h.view(ctx, d, account, sn, tpls)
}

// affectedPanel is one panel a template edit changed the games of, and
// whether the change left it with a conflict nobody has answered.
type affectedPanel struct {
	ThingName     string    `json:"thingName"`
	Name          string    `json:"name"`
	NeedsDecision bool      `json:"needsDecision"`
	Undecided     [][]int64 `json:"undecided"`
}

// affected reports the caller's panels that use template id, as they stand
// after the edit, and asks the director to run for each: a template edit
// changes what those panels show, and the change should be felt now. tpls
// is the caller's templates with the edit in them.
func (h *Handler) affected(ctx context.Context, sub, id string, tpls map[string][]int64, sn *season.Season) ([]affectedPanel, error) {
	devs, err := h.Store.ListByOwner(ctx, sub)
	if err != nil {
		return nil, err
	}
	out := []affectedPanel{}
	for _, d := range devs {
		if !uses(d, id) {
			continue
		}
		o := outcome(d.Schedule, tpls, sn)
		out = append(out, affectedPanel{ThingName: d.ThingName, Name: d.Name,
			NeedsDecision: len(o.Undecided) > 0, Undecided: o.Undecided})
		if h.Direct != nil {
			if err := h.Direct(ctx, d.ThingName); err != nil {
				slog.Warn("director not asked; the next minute will act", "thing", d.ThingName, "err", err)
			}
		}
	}
	return out, nil
}

// uses reports whether a panel's schedule names template id.
func uses(d devices.Device, id string) bool {
	for _, t := range d.Schedule.Templates {
		if t == id {
			return true
		}
	}
	return false
}

// find returns the caller's template with this id, from the list the store
// gave for the caller. There is no other way to reach a template from an id:
// one that is not in the caller's list does not exist, whoever holds it.
func find(list []templates.Template, id string) (templates.Template, bool) {
	for _, t := range list {
		if t.ID == id {
			return t, true
		}
	}
	return templates.Template{}, false
}

// templateBody reads a template request: size, then shape, then the name.
func templateBody(rawBody []byte) (templates.Template, events.APIGatewayV2HTTPResponse, bool) {
	if len(rawBody) > templates.MaxBody {
		res, _ := fail(400, "invalid template")
		return templates.Template{}, res, false
	}
	t, err := templates.Decode(rawBody)
	switch {
	case errors.Is(err, templates.ErrBadName):
		res, _ := fail(400, "invalid name")
		return templates.Template{}, res, false
	case err != nil:
		res, _ := fail(400, "invalid template")
		return templates.Template{}, res, false
	}
	return t, events.APIGatewayV2HTTPResponse{}, true
}

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

// templatesOf reads the caller's templates as the schedule rules take them.
// With no templates store wired (an older deployment) an account has none.
func (h *Handler) templatesOf(ctx context.Context, sub string) (map[string][]int64, error) {
	if h.Templates == nil {
		return map[string][]int64{}, nil
	}
	list, err := h.Templates.List(ctx, sub)
	if err != nil {
		return nil, err
	}
	return templates.GamesOf(list), nil
}

// scheduleInputs reads what these panels' schedule views need beyond their
// rows: the season, and the owner's templates when any panel has one. The
// reads are skipped when no panel has a schedule to explain. Either source
// down is a nil season, and the views say less (Known false) rather than
// show a kept set built without the template games in it.
func (h *Handler) scheduleInputs(ctx context.Context, sub string, panels []devices.Device) (*season.Season, map[string][]int64) {
	scheduled, templated := false, false
	for _, d := range panels {
		scheduled = scheduled || !d.Schedule.IsZero()
		templated = templated || len(d.Schedule.Templates) > 0
	}
	if !scheduled {
		return nil, nil
	}
	tpls := map[string][]int64{}
	if templated {
		// No store wired is the same failure as a store that cannot be read,
		// as the director treats it (internal/director): a row names
		// templates and their games cannot be had, so the view says less
		// rather than show a kept set the owner did not ask for. Only a row
		// written under a deployment that had the table can be in this state.
		if h.Templates == nil {
			slog.Warn("templates not configured; listing panels without what follows from their schedules")
			return nil, nil
		}
		var err error
		if tpls, err = h.templatesOf(ctx, sub); err != nil {
			slog.Warn("templates unavailable; listing panels without what follows from their schedules", "err", err)
			return nil, nil
		}
	}
	return h.seasonOrNil(ctx), tpls
}

func (h *Handler) view(ctx context.Context, d devices.Device, account *settings.Settings, sn *season.Season, tpls map[string][]int64) deviceView {
	v := deviceView{ThingName: d.ThingName, Name: d.Name, GameID: d.GameID, ChosenAt: d.ChosenAt,
		Wake:     d.Wake.Live(h.now()),
		Schedule: scheduleViewOf(d.Schedule, tpls, sn, h.now())}
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
		sn, tpls := h.scheduleInputs(ctx, sub, devs)
		out := make([]deviceView, 0, len(devs))
		for _, d := range devs {
			out = append(out, h.view(ctx, d, account, sn, tpls))
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
		// DecodeAccount, not Decode: an account has no orientation, and a
		// rotate key on this route is refused rather than stored where
		// Resolve would never read it.
		a, err := settings.DecodeAccount(rawBody)
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
		return respond(200, h.viewOne(ctx, d, &account))

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
		return respond(200, h.viewOne(ctx, d, &account))

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
		return respond(200, h.viewOne(ctx, d, &account))

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
		// Every template named is looked up under the caller, in the list
		// the store gives for their subject, before it is attached. One that
		// is not there is not theirs or does not exist, and those are the
		// same answer on purpose: not-yours is a 404, never a 403.
		tpls := map[string][]int64{}
		if len(asked.Templates) > 0 {
			if tpls, err = h.templatesOf(ctx, sub); err != nil {
				return fail(500, "lookup failed")
			}
			for _, id := range asked.Templates {
				if _, ok := tpls[id]; !ok {
					return fail(404, "no such template")
				}
			}
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
		all, _ := schedule.Candidates(asked, tpls)
		saved, err := schedule.Save(asked, d.Schedule, tpls, sn.Starts(all))
		switch {
		case errors.Is(err, schedule.ErrUnknownGame):
			return fail(400, "unknown game")
		case errors.Is(err, schedule.ErrBadResolution):
			return fail(400, "invalid resolution")
		case errors.Is(err, schedule.ErrUnknownTemplate):
			return fail(404, "no such template")
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
			"games", len(d.Schedule.Games), "templates", len(d.Schedule.Templates), "resolutions", len(d.Schedule.Resolutions))
		if h.Direct != nil {
			// The save is done; a director that cannot be reached is a
			// change felt within the minute, not a failed save.
			if err := h.Direct(ctx, d.ThingName); err != nil {
				slog.Warn("director not asked; the next minute will act", "thing", d.ThingName, "err", err)
			}
		}
		return respond(200, scheduleViewOf(d.Schedule, tpls, &sn, h.now()))

	case "GET /api/templates":
		// The caller's templates: one Query under their subject. There is
		// no route that takes an id and no owner.
		if h.Templates == nil {
			return fail(500, "templates are not configured")
		}
		list, err := h.Templates.List(ctx, sub)
		if err != nil {
			return fail(500, "list failed")
		}
		return respond(200, list)

	case "POST /api/templates":
		// Size, then shape, then the season, then the bound on the account.
		// The id is made here and never taken from the body.
		if h.Templates == nil {
			return fail(500, "templates are not configured")
		}
		t, res, ok := templateBody(rawBody)
		if !ok {
			return res, nil
		}
		if h.Season == nil {
			return fail(500, "no schedule source")
		}
		sn, err := h.Season(ctx)
		if err != nil {
			// Without the season a game id cannot be vouched for. Nothing is
			// stored on a guess.
			slog.Warn("season unavailable; template not saved", "err", err)
			return fail(502, "schedule unavailable")
		}
		// A new template has no previous list, so every id must be in the
		// season now: unknown ids are refused, never dropped quietly.
		if t.Games, err = schedule.CheckGames(t.Games, nil, sn.Starts(t.Games)); err != nil {
			return fail(400, "unknown game")
		}
		list, err := h.Templates.List(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		// The bound is read-then-write, not a condition on the table: two
		// creates racing could leave an account one over it. Twenty-one
		// templates is not a harm worth a transaction; a runaway client is
		// what the bound is for, and it stops one.
		if len(list) >= templates.MaxPerAccount {
			return respond(409, struct {
				Error string `json:"error"`
				Max   int    `json:"max"`
			}{"too many templates", templates.MaxPerAccount})
		}
		// A collision on a fresh id is one in 2^96; a second try covers a
		// broken NewID in a test, and after that something is wrong.
		for try := 0; try < 2; try++ {
			t.ID = h.newID()
			err = h.Templates.Create(ctx, sub, t)
			if !errors.Is(err, templates.ErrExists) {
				break
			}
		}
		if err != nil {
			return fail(500, "save failed")
		}
		slog.Info("template created", "sub", sub, "template", t.ID, "games", len(t.Games))
		return respond(201, t)

	case "PUT /api/templates/{id}":
		// The id from the path is only ever compared against the caller's
		// own list. The body replaces the name and the games; a game the
		// template already had that has since left the season is over and
		// is dropped, as on a panel; a new id not in the season is refused.
		if h.Templates == nil {
			return fail(500, "templates are not configured")
		}
		t, res, ok := templateBody(rawBody)
		if !ok {
			return res, nil
		}
		list, err := h.Templates.List(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		prev, ok := find(list, req.PathParameters["id"])
		if !ok {
			return fail(404, "no such template")
		}
		if h.Season == nil {
			return fail(500, "no schedule source")
		}
		sn, err := h.Season(ctx)
		if err != nil {
			slog.Warn("season unavailable; template not saved", "err", err)
			return fail(502, "schedule unavailable")
		}
		if t.Games, err = schedule.CheckGames(t.Games, prev.Games, sn.Starts(t.Games)); err != nil {
			return fail(400, "unknown game")
		}
		t.ID = prev.ID
		if err := h.Templates.Replace(ctx, sub, t); err != nil {
			if errors.Is(err, templates.ErrNotFound) {
				return fail(404, "no such template")
			}
			return fail(500, "save failed")
		}
		// Editing a template changes what several panels show. The answer
		// says which, and whether any now has a conflict the owner must
		// settle; the site (SCO-43) takes them through those. Nothing is
		// refused for it: the default rule stands in until they do, and the
		// panel is flagged until then.
		tpls := templates.GamesOf(list)
		tpls[t.ID] = t.Games
		panels, err := h.affected(ctx, sub, t.ID, tpls, &sn)
		if err != nil {
			// Saved, but the report could not be built: the site should
			// refresh rather than believe an empty list.
			return fail(500, "lookup failed")
		}
		slog.Info("template saved", "sub", sub, "template", t.ID, "games", len(t.Games), "panels", len(panels))
		return respond(200, struct {
			templates.Template
			Panels []affectedPanel `json:"panels"`
		}{t, panels})

	case "DELETE /api/templates/{id}":
		if h.Templates == nil {
			return fail(500, "templates are not configured")
		}
		list, err := h.Templates.List(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		t, ok := find(list, req.PathParameters["id"])
		if !ok {
			return fail(404, "no such template")
		}
		// A template a panel uses is not deleted: the panel would silently
		// show less. The owner detaches it first, or does not delete it.
		// Only the caller's panels can use it, since attaching one is done
		// under the caller, so their list is the whole count. The check is
		// read-then-write, like the per-account bound: a schedule save that
		// attaches this template between the list and the delete leaves a
		// panel naming a template that is gone. schedule.Candidates ignores
		// an id with no entry, so that panel shows less, never more, and the
		// owner's own two requests are the only way to race it.
		devs, err := h.Store.ListByOwner(ctx, sub)
		if err != nil {
			return fail(500, "lookup failed")
		}
		using := []string{}
		for _, d := range devs {
			if uses(d, t.ID) {
				using = append(using, d.ThingName)
			}
		}
		if len(using) > 0 {
			return respond(409, struct {
				Error  string   `json:"error"`
				Panels int      `json:"panels"`
				Things []string `json:"thingNames"`
			}{"template in use", len(using), using})
		}
		if err := h.Templates.Delete(ctx, sub, t.ID); err != nil {
			if errors.Is(err, templates.ErrNotFound) {
				return fail(404, "no such template")
			}
			return fail(500, "delete failed")
		}
		slog.Info("template deleted", "sub", sub, "template", t.ID)
		return respond(200, map[string]string{"id": t.ID})

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
		return respond(200, h.viewOne(ctx, d, nil))

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
