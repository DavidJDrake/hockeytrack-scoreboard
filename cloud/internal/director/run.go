package director

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"hockeytrack-scoreboard/internal/accounts"
	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/panelconfig"
	"hockeytrack-scoreboard/internal/reduce"
	"hockeytrack-scoreboard/internal/schedule"
	"hockeytrack-scoreboard/internal/season"
	"hockeytrack-scoreboard/internal/settings"
)

// MaxPublishes is the ceiling on publishes in one run. The fleet is a handful
// of panels and a run changes a panel's game at most once, so a run that
// wants more than this is a bad schedule source or a bad clock, not a busy
// night. The rest of the panels wait for the next minute, and the log line
// below is what terraform/director.tf alarms on.
const MaxPublishes = 25

// CeilingMessage is the log line the ceiling alarm's metric filter matches,
// quoted. Change both together.
const CeilingMessage = "publish ceiling reached; the remaining panels wait for the next run"

// lookback, plus the panel's hold, is how far behind now a game's start may
// be and still have its state read. A game that started earlier than the
// longest overtime plus the hold can neither be live nor inside its hold; it
// is over without a lookup.
const lookback = 8 * time.Hour

// Director is the loop. Its stores are interfaces so run_test.go can drive
// it with the fakes; cmd/director wires the real ones.
type Director struct {
	Devices devices.Store
	Games   gamestore.Store
	// Accounts holds each owner's default settings, laid under the panel's
	// own in the document exactly as the API lays them (its send). Nil means
	// nobody has any.
	Accounts accounts.Store
	Season   func(ctx context.Context) (season.Season, error)
	Pub      iotpub.Publisher
	Now      func() time.Time
}

// Run directs every claimed panel with a schedule, or only thing when it is
// named (the API names the panel whose schedule was just saved). A panel
// whose row cannot be read or whose game cannot be looked up is skipped with
// an error, not guessed at, and the others are still directed: the run
// returns the errors joined so the failure is counted.
func (d *Director) Run(ctx context.Context, thing string) error {
	panels, err := d.panels(ctx, thing)
	if err != nil || len(panels) == 0 {
		return err
	}
	// Without the season no kept set can be built, so nothing is sent: a
	// schedule source that is down leaves every panel where it is.
	sn, err := d.Season(ctx)
	if err != nil {
		return fmt.Errorf("season: %w", err)
	}
	now := d.Now()
	run := &run{d: d, season: sn, now: now, states: map[int64]*reduce.State{}}
	var errs []error
	for _, dev := range panels {
		if err := run.direct(ctx, dev); err != nil {
			slog.Error("panel not directed", "thing", dev.ThingName, "err", err)
			errs = append(errs, fmt.Errorf("%s: %w", dev.ThingName, err))
		}
	}
	return errors.Join(errs...)
}

func (d *Director) panels(ctx context.Context, thing string) ([]devices.Device, error) {
	if thing == "" {
		return d.Devices.ListScheduled(ctx)
	}
	dev, ok, err := d.Devices.Get(ctx, thing)
	if err != nil {
		return nil, err
	}
	// The name came from an event. It selects a row and nothing more: a
	// panel nobody owns, or with nothing asked for, is not directed.
	if !ok || dev.Owner == "" || dev.Schedule.IsZero() {
		return nil, nil
	}
	return []devices.Device{dev}, nil
}

// run is one invocation's scratch: the clock it runs on and the states it
// has read, so a game on several panels is read once.
type run struct {
	d         *Director
	season    season.Season
	now       time.Time
	states    map[int64]*reduce.State
	published int
	ceiling   bool
}

func (r *run) state(ctx context.Context, id int64) (*reduce.State, error) {
	if s, ok := r.states[id]; ok {
		return s, nil
	}
	s, found, err := r.d.Games.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	if !found {
		r.states[id] = nil
		return nil, nil
	}
	r.states[id] = &s
	return &s, nil
}

// game builds what the rule knows about one game. A start comes from the
// season, or from the reducer's document for a game the season no longer
// lists; with neither the game is absent, which the rule reads as over.
func (r *run) game(ctx context.Context, id int64, start string, hold time.Duration) (Game, bool, error) {
	var s *reduce.State
	st, ok := parse(start)
	// Only a game whose slot could still be on is looked up. A start far
	// enough behind now is over without a read; one ahead of now cannot be
	// live yet, and the rule treats it as upcoming either way.
	if !ok || (!st.Before(r.now.Add(-lookback-hold)) && !st.After(r.now)) {
		var err error
		if s, err = r.state(ctx, id); err != nil {
			return Game{}, false, err
		}
	}
	if !ok && s != nil {
		st, ok = parse(s.Start)
	}
	if !ok {
		return Game{}, false, nil
	}
	g := Game{Start: st}
	if s != nil {
		g.State = s.GameState
		if s.GameState == "FINAL" {
			// finalAt is set by the reducer since SCO-53; a game that went
			// final before then has its last heartbeat instead, which stopped
			// within seconds of the horn (design section 2a).
			g.FinalAt = time.UnixMilli(max(s.FinalAt, s.AsOf, s.SeenAt))
		}
	}
	return g, true, nil
}

func parse(start string) (time.Time, bool) {
	if start == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, start)
	return t, err == nil
}

func (r *run) direct(ctx context.Context, dev devices.Device) error {
	// The account's defaults are read before anything is decided: the
	// document carries the settings, and one composed without the account's
	// layer would wipe those settings from the panel. No defaults readable,
	// nothing sent.
	account := settings.Settings{}
	if r.d.Accounts != nil {
		var err error
		if account, err = r.d.Accounts.Defaults(ctx, dev.Owner); err != nil {
			return fmt.Errorf("account defaults: %w", err)
		}
	}
	resolved, _ := settings.Resolve(account, dev.Display)

	// The kept set, the same way the API shows it to the owner. Nothing the
	// rule returns can be outside it except what the owner put there.
	starts := r.season.Starts(dev.Schedule.Games)
	out := schedule.Resolve(dev.Schedule, dev.Schedule.Games, nil, starts)
	p := Panel{Kept: out.Kept, Games: map[int64]Game{}, Hold: time.Duration(resolved.FinalHoldMin) * time.Minute,
		Showing: dev.GameID, Sent: dev.Sent}
	for _, id := range out.Kept {
		g, ok, err := r.game(ctx, id, starts[id], p.Hold)
		if err != nil {
			return fmt.Errorf("game %d: %w", id, err)
		}
		if ok {
			p.Games[id] = g
		}
	}
	if dev.GameID != 0 {
		if _, ok := p.Games[dev.GameID]; !ok {
			var start string
			if g, found := r.season.Find(dev.GameID); found {
				start = g.Start
			}
			g, ok, err := r.game(ctx, dev.GameID, start, p.Hold)
			if err != nil {
				return fmt.Errorf("game %d: %w", dev.GameID, err)
			}
			if ok {
				p.Games[dev.GameID] = g
			}
		}
	}

	current := Current(p, r.now)
	if current == 0 || current == dev.GameID {
		// Nothing ahead, or the panel already has it (the owner's override
		// included): nothing to send and nothing to write. This is what
		// makes a retry converge and an idle minute cost nothing.
		return nil
	}
	// The rule promises this; the check makes a broken rule fail closed
	// rather than send a panel a game its owner never chose.
	if !contains(out.Kept, current) {
		return fmt.Errorf("rule chose %d, which is not kept", current)
	}
	if r.published >= MaxPublishes {
		if !r.ceiling {
			r.ceiling = true
			slog.Warn(CeilingMessage, "ceiling", MaxPublishes)
		}
		return nil
	}
	// The document is built by the API's function and nothing else, so
	// there is still one author of the wire format. chosenAt is now: the
	// director publishes only on a change of game, and a change of game is
	// a choice (design section 6).
	chosenAt := r.now.UnixMilli()
	payload, err := panelconfig.Compose(current, chosenAt, resolved, dev.Wake.Live(r.now))
	if err != nil {
		return err
	}
	if err := r.d.Pub.Publish(ctx, panelconfig.Topic(dev.ThingName), payload, true); err != nil {
		return fmt.Errorf("publish: %w", err)
	}
	r.published++
	// Publish, then record. If the record fails the next run sees the row
	// still naming the old game, sends the same document again and records
	// again: a duplicate, never a panel the row disagrees with. The write is
	// conditioned on the owner, so the row of a panel released between the
	// read and here is not handed a game back. The panel itself is: the
	// publish above came first, so a released panel can carry one stale
	// retained document until its next claimant's first send replaces it.
	// A release publishes nothing today, so that panel already holds the
	// prior owner's document either way; the row is what this protects.
	err = r.d.Devices.MarkSent(ctx, dev.ThingName, dev.Owner, current, chosenAt)
	if errors.Is(err, devices.ErrNotOwner) {
		slog.Warn("panel changed hands during the run; nothing recorded", "thing", dev.ThingName)
		return nil
	}
	if err != nil {
		return fmt.Errorf("record: %w", err)
	}
	slog.Info("panel directed", "thing", dev.ThingName, "from", dev.GameID, "to", current)
	return nil
}

func contains(ids []int64, id int64) bool {
	for _, k := range ids {
		if k == id {
			return true
		}
	}
	return false
}
