// Package sweep is the backstop behind the Released list (SCO-33, step 5 of
// retiring a panel): once a day, find every panel that nobody owns and that
// the broker has not seen for a year, and retire it.
//
// THIS RUN IT ONLY LOOKS. The retire action is SCO-32's Lambda, which does
// not exist yet, and the ticket requires a full dry-run cycle before the
// sweep may act in any case. So Run logs, one line per panel, what it would
// retire, and there is no code path here that deletes, deactivates or
// unbinds anything; terraform/sweep.tf gives the function's role no
// permission that could. When SCO-32 lands, act mode is a deliberate second
// step: a new grant on the role, this package calling that Lambda, its role
// added by name to step 4's alarm allow list, and a switch that defaults to
// dry-run. None of that is here.
package sweep

import (
	"context"
	"log/slog"
	"sort"
	"time"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/presence"
)

// Unseen is how long a panel must have been off the broker before the sweep
// may name it: the owner's figure, long enough that a panel boxed up for a
// season, or in a drawer between owners, is never caught.
const Unseen = 365 * 24 * time.Hour

// MaxRetire is the ceiling on panels counted in one run, so a bad clock or a
// bad query cannot empty the fleet in a night. It holds in dry-run too, so
// the would-retire count of the dry-run cycle is exactly what act mode would
// have done. In act mode the panels past the cut would wait for the next
// day's run; in dry-run nothing changes between runs, so the same MaxRetire
// by name would be cut every day and the rest would never appear in the
// record at all. So Run logs the rest too, under a different line that the
// would-retire metric does not count. The ceiling line is what
// terraform/sweep.tf alarms on.
const MaxRetire = 5

// WouldRetireMessage is the log line written once per panel the rule
// selects, up to MaxRetire. terraform/sweep.tf counts it, quoted, into a
// metric so the owner sees every selection of the dry-run cycle. Change both
// together.
const WouldRetireMessage = "would retire"

// CeilingMessage is the log line the ceiling alarm's metric filter matches,
// quoted. Change both together.
const CeilingMessage = "retire ceiling reached; the rest are logged as past the ceiling"

// PastCeilingMessage is the log line written once per panel the rule
// selected past the cut, so the dry-run record names every panel the rule
// found. A metric filter's quoted phrase matches anywhere in the event, so
// this line must not contain WouldRetireMessage or the ceiling phrase, or
// the counts would be wrong; sweep_test.go holds that.
const PastCeilingMessage = "past the ceiling; named for the record only"

// Candidate is one panel the rule selected and the fact it was selected on.
type Candidate struct {
	ThingName string
	LastSeen  time.Time
}

// Select is the rule, pure so it can be tested as a table: a panel is a
// candidate when nobody owns it, nobody released it through the Released
// list (that path handles its own), the broker has a record of it, it is
// not on the broker now, and that record is older than Unseen. A panel the
// broker has no record of is never selected: "unknown" is not "long ago",
// and a panel that has never connected since indexing began is exactly the
// case a fresh index produces for every panel on its first day. The result
// is sorted by name and cut at MaxRetire; past is what fell past the cut,
// so a non-empty past is the ceiling having been reached.
func Select(devs []devices.Device, seen map[string]presence.Seen, now time.Time) (chosen, past []Candidate) {
	for _, d := range devs {
		if d.Owner != "" || d.ReleasedBy != "" {
			continue
		}
		s, ok := seen[d.ThingName]
		if !ok || s.At.IsZero() || s.Connected {
			continue
		}
		if now.Sub(s.At) <= Unseen {
			continue
		}
		chosen = append(chosen, Candidate{ThingName: d.ThingName, LastSeen: s.At})
	}
	sort.Slice(chosen, func(i, j int) bool { return chosen[i].ThingName < chosen[j].ThingName })
	if len(chosen) > MaxRetire {
		return chosen[:MaxRetire], chosen[MaxRetire:]
	}
	return chosen, nil
}

// Sweep is one run's dependencies. Its stores are interfaces so sweep_test.go
// can drive it with the fakes; cmd/sweep wires the real ones.
type Sweep struct {
	Devices  devices.Store
	Presence presence.Source
	// Now is the clock the rule measures Unseen against. Injected so a test
	// can move it; nil means time.Now.
	Now func() time.Time
}

func (s *Sweep) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// Run reads the unowned panels and the broker's record of them, applies
// Select, and logs. It returns the candidates it logged so a caller can see
// what a run found; it acts on none of them (see the package comment). An
// error from either read stops the run: a sweep that judged on half the
// facts would be worse than one that said nothing, and the function's error
// alarm says so.
func (s *Sweep) Run(ctx context.Context) ([]Candidate, error) {
	devs, err := s.Devices.ListUnowned(ctx)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(devs))
	for _, d := range devs {
		names = append(names, d.ThingName)
	}
	seen := map[string]presence.Seen{}
	if len(names) > 0 {
		if seen, err = s.Presence.Lookup(ctx, names); err != nil {
			return nil, err
		}
	}
	now := s.now()
	chosen, past := Select(devs, seen, now)
	for _, c := range chosen {
		slog.Warn(WouldRetireMessage, "mode", "dry-run", "thing", c.ThingName,
			"lastSeen", c.LastSeen.UTC().Format(time.RFC3339), "unseenDays", int(now.Sub(c.LastSeen).Hours()/24))
	}
	if len(past) > 0 {
		slog.Warn(CeilingMessage, "ceiling", MaxRetire, "past", len(past))
	}
	// Named so the dry-run record is complete, and not counted: act mode
	// would not have reached these today.
	for _, c := range past {
		slog.Warn(PastCeilingMessage, "mode", "dry-run", "thing", c.ThingName,
			"lastSeen", c.LastSeen.UTC().Format(time.RFC3339), "unseenDays", int(now.Sub(c.LastSeen).Hours()/24))
	}
	slog.Info("sweep complete", "mode", "dry-run", "unowned", len(devs), "known", len(seen), "wouldRetire", len(chosen), "pastCeiling", len(past))
	return chosen, nil
}
