package schedule

import (
	"bytes"
	"encoding/json"
	"errors"
	"sort"
)

// Bounds, from the design (section 8). They are what keeps a panel's row,
// and the work done on it each minute, a known size.
const (
	MaxGames     = 1500
	MaxTemplates = 5
)

// Resolution is an owner's answer to one conflict: of the games in Sequence,
// show Keep. It is stored against the whole sequence, so that if the conflict
// changes in any way the answer stops applying rather than being stretched
// to fit (see Key).
type Resolution struct {
	Sequence []int64 `json:"sequence"`
	Keep     []int64 `json:"keep"`
}

// Panel is what an owner has asked a panel to show: its own games, the
// templates attached to it in priority order, and the conflicts answered.
type Panel struct {
	Games       []int64      `json:"games"`
	Templates   []string     `json:"templates"`
	Resolutions []Resolution `json:"resolutions"`
}

// IsZero reports whether nothing has been asked for.
func (p Panel) IsZero() bool {
	return len(p.Games) == 0 && len(p.Templates) == 0 && len(p.Resolutions) == 0
}

var (
	// ErrMalformed covers everything wrong with the request itself.
	ErrMalformed = errors.New("schedule: malformed")
	// ErrUnknownGame is an id that is not in the season and was not already
	// on the panel.
	ErrUnknownGame = errors.New("schedule: unknown game")
	// ErrBadResolution is an answer that keeps a game outside its sequence,
	// keeps nothing, or keeps two games that overlap.
	ErrBadResolution = errors.New("schedule: invalid resolution")
)

// Decode reads a request body strictly: unknown keys, trailing data, too
// many entries, an id that is not positive or appears twice are all
// ErrMalformed. Nulls become empty lists.
func Decode(body []byte) (Panel, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var p Panel
	if err := dec.Decode(&p); err != nil || dec.More() {
		return Panel{}, ErrMalformed
	}
	if len(p.Games) > MaxGames || len(p.Templates) > MaxTemplates || len(p.Resolutions) > MaxGames/2 {
		return Panel{}, ErrMalformed
	}
	if !distinctPositive(p.Games) {
		return Panel{}, ErrMalformed
	}
	for _, r := range p.Resolutions {
		if len(r.Sequence) < 2 || len(r.Sequence) > MaxGames || len(r.Keep) > len(r.Sequence) ||
			!distinctPositive(r.Sequence) || !distinctPositive(r.Keep) {
			return Panel{}, ErrMalformed
		}
	}
	return p.normal(), nil
}

func distinctPositive(ids []int64) bool {
	seen := make(map[int64]bool, len(ids))
	for _, id := range ids {
		if id <= 0 || seen[id] {
			return false
		}
		seen[id] = true
	}
	return true
}

func (p Panel) normal() Panel {
	if p.Games == nil {
		p.Games = []int64{}
	}
	if p.Templates == nil {
		p.Templates = []string{}
	}
	if p.Resolutions == nil {
		p.Resolutions = []Resolution{}
	}
	return p
}

// Stored is the attribute a panel's row carries. Load is its inverse, and
// reads anything it cannot as "nothing asked for": a damaged attribute must
// not take a panel off its owner's list.
func Stored(p Panel) string {
	b, err := json.Marshal(p.normal())
	if err != nil {
		return "{}"
	}
	return string(b)
}

func Load(stored string) Panel {
	if stored == "" {
		return Panel{}.normal()
	}
	p, err := Decode([]byte(stored))
	if err != nil {
		return Panel{}.normal()
	}
	return p
}

// Outcome is what the rules make of a panel's schedule right now.
type Outcome struct {
	// Kept is what the panel may show, in start order, no two overlapping.
	Kept []int64
	// Undecided are the conflicts with no valid answer on record. While
	// there are any, the default rule has filled in for the owner and the
	// panel needs a decision.
	Undecided [][]int64
	// Unplaced are games with no readable start in the season given: past
	// games that have left the schedule, which is ordinary, or ids that
	// were never in it.
	Unplaced []int64
}

// Resolve applies the stored answers to the conflicts that exist NOW. An
// answer counts only if it is for exactly this sequence and is still valid
// against the starts given -- the NHL moves games. Otherwise the default
// rule decides that sequence, and it is reported as undecided.
//
// starts holds the start of every game that has one. sources, if not nil,
// says where a game came from, for the default rule's ranking; a game not in
// it is the panel's own.
func Resolve(p Panel, games []int64, sources map[int64]string, starts map[int64]string) Outcome {
	in := make([]Game, 0, len(games))
	for _, id := range games {
		src, ok := sources[id]
		if !ok {
			src = PanelSource
		}
		in = append(in, Game{ID: id, Start: starts[id], Source: src})
	}
	plan := Build(in, p.Templates)
	answers := map[string][]int64{}
	for _, r := range p.Resolutions {
		answers[Key(r.Sequence)] = r.Keep
	}
	out := Outcome{Unplaced: plan.Unreadable, Undecided: [][]int64{}}
	conflicted, defaulted := map[int64]bool{}, map[int64]bool{}
	for _, seq := range plan.Sequences {
		for _, id := range seq {
			conflicted[id] = true
		}
	}
	for _, id := range plan.DefaultKept {
		defaulted[id] = true
	}
	keep := map[int64]bool{}
	for _, id := range plan.DefaultKept {
		if !conflicted[id] {
			keep[id] = true
		}
	}
	for _, seq := range plan.Sequences {
		if answer, ok := answers[Key(seq)]; ok && ValidResolution(seq, answer, starts) {
			for _, id := range answer {
				keep[id] = true
			}
			continue
		}
		out.Undecided = append(out.Undecided, seq)
		for _, id := range seq {
			if defaulted[id] {
				keep[id] = true
			}
		}
	}
	out.Kept = make([]int64, 0, len(keep))
	for id := range keep {
		out.Kept = append(out.Kept, id)
	}
	sort.Slice(out.Kept, func(i, j int) bool {
		a, b := out.Kept[i], out.Kept[j]
		if starts[a] != starts[b] {
			return starts[a] < starts[b]
		}
		return a < b
	})
	return out
}

// Saved is the result of checking a schedule an owner is trying to save.
type Saved struct {
	// Panel is what to store: past games dropped, answers that no longer
	// match a conflict dropped, everything in a fixed order.
	Panel Panel
	// Unresolved are the conflicts among these games that the request did
	// not answer. If there are any, nothing may be stored: the owner is
	// right there, and nothing is decided for them (decision 8).
	Unresolved [][]int64
}

// Save checks a request against the season. previous is what the panel has
// now: a game on it that has since left the schedule is over, and is dropped
// without complaint; an id that is in neither is refused, because it is
// something this server has never vouched for.
func Save(req Panel, previous Panel, starts map[int64]string) (Saved, error) {
	had := map[int64]bool{}
	for _, id := range previous.Games {
		had[id] = true
	}
	games := make([]int64, 0, len(req.Games))
	for _, id := range req.Games {
		if _, ok := starts[id]; ok {
			games = append(games, id)
		} else if !had[id] {
			return Saved{}, ErrUnknownGame
		}
	}
	sort.Slice(games, func(i, j int) bool { return games[i] < games[j] })

	plan := Build(toGames(games, starts), nil)
	current := map[string][]int64{}
	for _, seq := range plan.Sequences {
		current[Key(seq)] = seq
	}
	out := Saved{Panel: Panel{Games: games, Templates: []string{}, Resolutions: []Resolution{}}, Unresolved: [][]int64{}}
	answered := map[string]bool{}
	for _, r := range req.Resolutions {
		key := Key(r.Sequence)
		seq, ok := current[key]
		if !ok {
			continue // an answer to a conflict that no longer exists
		}
		if answered[key] || !ValidResolution(seq, r.Keep, starts) {
			return Saved{}, ErrBadResolution
		}
		answered[key] = true
		keep := append([]int64(nil), r.Keep...)
		sort.Slice(keep, func(i, j int) bool { return keep[i] < keep[j] })
		sorted := append([]int64(nil), seq...)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
		out.Panel.Resolutions = append(out.Panel.Resolutions, Resolution{Sequence: sorted, Keep: keep})
	}
	for _, seq := range plan.Sequences {
		if !answered[Key(seq)] {
			out.Unresolved = append(out.Unresolved, seq)
		}
	}
	sort.Slice(out.Panel.Resolutions, func(i, j int) bool {
		return Key(out.Panel.Resolutions[i].Sequence) < Key(out.Panel.Resolutions[j].Sequence)
	})
	return out, nil
}

func toGames(ids []int64, starts map[int64]string) []Game {
	out := make([]Game, 0, len(ids))
	for _, id := range ids {
		out = append(out, Game{ID: id, Start: starts[id], Source: PanelSource})
	}
	return out
}
