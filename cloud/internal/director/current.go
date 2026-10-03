// Package director works out which of a panel's kept games is current and
// sends it (design section 6). The panel does not change: it still receives
// one gameId. This is what turns a schedule into that one id, once a minute.
//
// The rule is in this file and is pure: times and states in, an id out. The
// loop that reads the tables and publishes is in run.go, and cmd/director
// only wires it to AWS.
package director

import (
	"time"

	"hockeytrack-scoreboard/internal/schedule"
)

// Game is what the rule knows about one game: when it starts, from the
// season, and what the reducer last said about it.
type Game struct {
	Start time.Time
	// State is the reducer's PRE, LIVE or FINAL, or "" for a game the reducer
	// has not seen. A game it has not seen is upcoming until its slot has
	// passed, and over after that: a game nobody tracked is not waited on
	// forever.
	State string
	// FinalAt is when the game ended. Zero unless State is FINAL.
	FinalAt time.Time
}

// Panel is one panel's situation, as the rule sees it.
type Panel struct {
	// Kept are the games the panel may show, in start order, none
	// overlapping: schedule.Resolve's Kept. The rule returns an id from this
	// list, Showing, or 0; never anything else. That is what keeps a panel
	// from being sent a game its owner did not choose, and current_test.go
	// checks it.
	Kept []int64
	// Games is what is known about each game the rule may need: every kept
	// game and Showing. An id absent here is over, not an error: the reducer
	// never saw it and the season no longer lists it.
	Games map[int64]Game
	// Hold is the final-score hold in force on this panel.
	Hold time.Duration
	// Showing is the game the panel was last sent, by anyone: the row's
	// gameId. 0 means none.
	Showing int64
	// Sent is the game this director last sent to the panel, 0 for never.
	// When Showing is not Sent, the owner pressed "Show on panel" since, and
	// that is the override.
	Sent int64
}

// over reports whether a game is finished with: absent, final past its hold,
// or never seen once its slot has passed.
func (p Panel) over(id int64, now time.Time) bool {
	g, ok := p.Games[id]
	if !ok {
		return true
	}
	switch g.State {
	case "LIVE":
		return false
	case "FINAL":
		return !now.Before(g.FinalAt.Add(p.Hold))
	default:
		return !now.Before(g.Start.Add(schedule.Occupies))
	}
}

func (p Panel) state(id int64) string { return p.Games[id].State }

// Current is the rule (design section 6, and the owner's rulings in section
// 12). In order:
//
//  1. The owner's override: a game they put on the panel themselves wins
//     until it is over. The director never sends it -- the API already did
//     -- it only stands aside.
//  2. A kept game that is live, even past its 2h40: the one already showing
//     if it is live, else the earliest.
//  3. The most recent kept final, while now is before both the end of its
//     hold and the next kept game's puck drop.
//  4. The next kept game.
//  5. Nothing: 0. The panel takes the last final down by its own rule.
func Current(p Panel, now time.Time) int64 {
	if p.Showing != 0 && p.Showing != p.Sent && !p.over(p.Showing, now) {
		return p.Showing
	}
	kept := map[int64]bool{}
	for _, id := range p.Kept {
		kept[id] = true
	}
	if kept[p.Showing] && p.state(p.Showing) == "LIVE" {
		return p.Showing
	}
	for _, id := range p.Kept {
		if p.state(id) == "LIVE" {
			return id
		}
	}
	var final, next int64
	for _, id := range p.Kept {
		if p.over(id, now) {
			continue
		}
		if p.state(id) == "FINAL" {
			// Kept is in start order, so the last final seen is the most
			// recent unless the reducer says one ended after it.
			if final == 0 || !p.Games[id].FinalAt.Before(p.Games[final].FinalAt) {
				final = id
			}
			continue
		}
		if next == 0 {
			next = id
		}
	}
	if final != 0 && (next == 0 || now.Before(p.Games[next].Start)) {
		return final
	}
	return next
}

// Next is the strip's "up next" (SCO-56): the first kept game whose start is
// after the current game's end, or after now when nothing is current. 0 for
// none. Like Current it names a kept game or nothing, never anything else,
// and the game the panel is being sent is never its own next.
//
// A game's end is its final (FinalAt) or, before that, the end of its slot
// (schedule.Occupies from its start); a live game running past its slot has
// no known end, so now stands in. A kept game that has already started is
// not "next" whatever the current game is doing: the strip would be naming
// a puck drop that has passed.
func Next(p Panel, now time.Time) int64 {
	current := Current(p, now)
	// end is when the current game is done with, or zero when nothing is
	// current, which no real start is before.
	var end time.Time
	if g, ok := p.Games[current]; ok && current != 0 {
		end = g.Start.Add(schedule.Occupies)
		if g.State == "FINAL" {
			end = g.FinalAt
		}
	}
	for _, id := range p.Kept {
		if id == current {
			continue
		}
		g, ok := p.Games[id]
		if !ok || g.State == "LIVE" || g.State == "FINAL" {
			continue
		}
		// Strictly after now, but at or after the end: schedule.Overlap lets
		// an owner keep two games exactly Occupies apart ("touching exactly is
		// not overlapping"), so a start equal to the current game's end is
		// the next game, not one to skip past.
		if g.Start.After(now) && !g.Start.Before(end) {
			return id
		}
	}
	return 0
}
