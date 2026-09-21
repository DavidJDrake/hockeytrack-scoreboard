// Package schedule works out which of a panel's chosen games overlap, and
// what the panel shows until its owner decides.
//
// A panel shows one game at a time. Its games come from the panel itself and
// from templates, so two can be on at once; that is a conflict, and the owner
// resolves it. This package finds the conflicts, checks the owner's answers,
// and supplies the answer used until there is one, so that a panel is never
// undecided.
//
// The site has a copy of these rules (site/assets/overlap.js) so the picker
// can show conflicts as boxes are ticked. This is the copy that counts, and
// testdata/overlap-vectors.json is run against both.
package schedule

import (
	"sort"
	"strconv"
	"strings"
	"time"
)

// Occupies is how long a game holds a panel from its start: regulation
// length, the owner's figure. A game that runs long is not cut off for the
// next one -- a live game stays current until it ends -- so this only
// decides what counts as a conflict when games are chosen.
const Occupies = 160 * time.Minute

// PanelSource is the Source of a game set on the panel itself. Anything else
// is a template id.
const PanelSource = "panel"

type Game struct {
	ID     int64
	Start  string // RFC 3339, with a zone
	Source string
}

type placed struct {
	id    int64
	start time.Time
	rank  int
}

// Plan is everything the rules say about a set of chosen games.
type Plan struct {
	// Sequences are the conflicts: maximal chains of games joined by
	// overlaps, each listed by start then id. A game with no overlap is in
	// none of them.
	Sequences [][]int64
	// DefaultKept is what the panel shows until the owner decides, in start
	// order: every game outside a conflict, and from each conflict the games
	// the default rule keeps.
	DefaultKept []int64
	// Unreadable are games whose start is not an instant. They cannot be
	// placed, so they cannot be shown; they are reported, never dropped
	// quietly.
	Unreadable []int64
}

func parseStart(s string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

func overlaps(a, b time.Time) bool {
	if b.Before(a) {
		a, b = b, a
	}
	return b.Before(a.Add(Occupies)) // touching exactly is not overlapping
}

// Build applies the rules. order is the owner's template ids, highest
// priority first; the panel's own games outrank all of them, and a template
// that is not in the list ranks last.
func Build(games []Game, order []string) Plan {
	rankOf := func(source string) int {
		if source == PanelSource {
			return 0
		}
		for i, id := range order {
			if id == source {
				return i + 1
			}
		}
		return len(order) + 1
	}

	// One entry per game id, at the best priority it arrives with: the same
	// game from two sources is one game, and is not in conflict with itself.
	byID := map[int64]placed{}
	unreadable := map[int64]bool{}
	for _, g := range games {
		start, ok := parseStart(g.Start)
		if !ok {
			unreadable[g.ID] = true
			continue
		}
		p := placed{g.ID, start, rankOf(g.Source)}
		if have, seen := byID[g.ID]; !seen || p.rank < have.rank {
			byID[g.ID] = p
		}
	}
	all := make([]placed, 0, len(byID))
	for id, p := range byID {
		delete(unreadable, id) // readable from one source is readable
		all = append(all, p)
	}
	sort.Slice(all, func(i, j int) bool {
		if !all[i].start.Equal(all[j].start) {
			return all[i].start.Before(all[j].start)
		}
		return all[i].id < all[j].id
	})

	plan := Plan{Sequences: [][]int64{}, DefaultKept: []int64{}, Unreadable: []int64{}}
	for id := range unreadable {
		plan.Unreadable = append(plan.Unreadable, id)
	}
	sort.Slice(plan.Unreadable, func(i, j int) bool { return plan.Unreadable[i] < plan.Unreadable[j] })

	// Sweep in start order. A game joins the current chain if it starts
	// before the chain's latest end; otherwise the chain is closed.
	var chain []placed
	var chainEnd time.Time
	var kept []placed
	flush := func() {
		if len(chain) > 1 {
			ids := make([]int64, len(chain))
			for i, p := range chain {
				ids[i] = p.id
			}
			plan.Sequences = append(plan.Sequences, ids)
		}
		kept = append(kept, defaultKeep(chain)...)
		chain = nil
	}
	for _, p := range all {
		if len(chain) > 0 && !p.start.Before(chainEnd) {
			flush()
		}
		chain = append(chain, p)
		if end := p.start.Add(Occupies); end.After(chainEnd) {
			chainEnd = end
		}
	}
	flush()

	sort.Slice(kept, func(i, j int) bool {
		if !kept[i].start.Equal(kept[j].start) {
			return kept[i].start.Before(kept[j].start)
		}
		return kept[i].id < kept[j].id
	})
	for _, p := range kept {
		plan.DefaultKept = append(plan.DefaultKept, p.id)
	}
	return plan
}

// defaultKeep is the rule used until the owner decides: take the games in
// order of priority, then start, then id, keeping each one that does not
// overlap a game already kept. Deterministic, and boring on purpose.
func defaultKeep(chain []placed) []placed {
	byPriority := append([]placed(nil), chain...)
	sort.Slice(byPriority, func(i, j int) bool {
		a, b := byPriority[i], byPriority[j]
		if a.rank != b.rank {
			return a.rank < b.rank
		}
		if !a.start.Equal(b.start) {
			return a.start.Before(b.start)
		}
		return a.id < b.id
	})
	var kept []placed
	for _, p := range byPriority {
		clear := true
		for _, k := range kept {
			if overlaps(p.start, k.start) {
				clear = false
				break
			}
		}
		if clear {
			kept = append(kept, p)
		}
	}
	return kept
}

// ValidResolution reports whether kept is an acceptable answer to one
// conflict: at least one game, every one of them from the sequence, none
// twice, and no two overlapping. starts maps each game id in the sequence to
// its start.
func ValidResolution(sequence, kept []int64, starts map[int64]string) bool {
	if len(kept) == 0 {
		return false
	}
	in := map[int64]bool{}
	for _, id := range sequence {
		in[id] = true
	}
	seen := map[int64]bool{}
	var times []time.Time
	for _, id := range kept {
		if !in[id] || seen[id] {
			return false
		}
		seen[id] = true
		t, ok := parseStart(starts[id])
		if !ok {
			return false
		}
		for _, other := range times {
			if overlaps(t, other) {
				return false
			}
		}
		times = append(times, t)
	}
	return true
}

// Key names a sequence, so an answer can be stored against the conflict it
// was given for. If the conflict changes in any way -- a game added, removed
// or moved into another's time -- its key changes and the old answer no
// longer applies: an answer is never reinterpreted to fit a different
// question.
func Key(sequence []int64) string {
	ids := append([]int64(nil), sequence...)
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	parts := make([]string, len(ids))
	for i, id := range ids {
		parts[i] = strconv.FormatInt(id, 10)
	}
	return strings.Join(parts, "-")
}
