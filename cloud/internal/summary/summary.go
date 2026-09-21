// Package summary builds the one small document that says what every game
// today stands at: who, the score, and where in the game.
//
// It exists for the panel's information strip, which shows one line about
// some other game. A panel is allowed to subscribe to any game's state, but
// a dozen live games is a dozen heartbeats every five seconds, per panel,
// to draw a dozen characters. This is one retained message a minute.
//
// Everything here ends up drawn on a panel in somebody's house, and begins
// as text in a feed this project does not control. So the document is built
// from what passes a check, not from what arrived: a row with anything
// unreadable in it is left out whole, and the document has a fixed ceiling.
package summary

import (
	"sort"
	"time"

	"hockeytrack-scoreboard/internal/reduce"
)

// V is the document's version. A panel that does not know a version shows
// nothing from it.
const V = 1

// MaxGames bounds the document. The busiest NHL day is sixteen games; a
// table holding more than this many current ones is wrong about something,
// and a panel is not where to find out.
const MaxGames = 32

const (
	// A final stays in the summary this long after the game ended.
	finalFor = 6 * time.Hour
	// A game that has not started is listed from this long before its start
	// until this long after it (a late start is still about to happen; a
	// row still PRE hours later was postponed, or never polled).
	preAhead  = 24 * time.Hour
	preBehind = 3 * time.Hour
)

type Game struct {
	GameID    int64  `json:"gameId"`
	Away      string `json:"away"`
	Home      string `json:"home"`
	AwayScore int    `json:"awayScore"`
	HomeScore int    `json:"homeScore"`
	State     string `json:"state"` // PRE | LIVE | FINAL
	// Period is the label a panel would draw ("2", "OT", "SO"); empty
	// before the game. For a final it says how the game ended.
	Period       string `json:"period,omitempty"`
	Intermission bool   `json:"intermission,omitempty"`
	Start        string `json:"start,omitempty"`
	// SeenAt is when the feed last confirmed a live game, so a panel can
	// tell a score from a score nobody has checked for an hour.
	SeenAt int64 `json:"seenAt,omitempty"`
}

type Doc struct {
	V     int    `json:"v"`
	AsOf  int64  `json:"asOf"`
	Games []Game `json:"games"`
}

// Build makes the document from the rows the reducer keeps. It never fails:
// a row it cannot vouch for is dropped, and no rows is a document too -- an
// empty one, which is how a panel learns that last night's scores are over.
func Build(states []reduce.State, now time.Time) Doc {
	doc := Doc{V: V, AsOf: now.UnixMilli(), Games: []Game{}}
	for _, s := range states {
		if g, ok := row(s, now); ok {
			doc.Games = append(doc.Games, g)
		}
	}
	sort.Slice(doc.Games, func(i, j int) bool {
		a, b := doc.Games[i], doc.Games[j]
		if a.Start != b.Start {
			return a.Start < b.Start
		}
		return a.GameID < b.GameID
	})
	if len(doc.Games) > MaxGames {
		doc.Games = doc.Games[:MaxGames]
	}
	return doc
}

func row(s reduce.State, now time.Time) (Game, bool) {
	if s.GameID <= 0 || !abbrev(s.Away.Abbrev) || !abbrev(s.Home.Abbrev) || s.Away.Abbrev == s.Home.Abbrev {
		return Game{}, false
	}
	if !score(s.Away.Score) || !score(s.Home.Score) {
		return Game{}, false
	}
	start, startOK := time.Time{}, false
	if t, err := time.Parse(time.RFC3339, s.Start); err == nil {
		start, startOK = t.UTC(), true
	}
	g := Game{GameID: s.GameID, Away: s.Away.Abbrev, Home: s.Home.Abbrev, State: s.GameState}
	if startOK {
		// Written back out rather than passed through: what a panel gets is
		// a time this code read, in one spelling.
		g.Start = start.Format(time.RFC3339)
	}
	switch s.GameState {
	case "PRE":
		if !startOK || start.Before(now.Add(-preBehind)) || start.After(now.Add(preAhead)) {
			return Game{}, false
		}
		return g, true
	case "LIVE":
		g.AwayScore, g.HomeScore = s.Away.Score, s.Home.Score
		g.Period = label(s.Period.Label)
		g.Intermission = s.Clock.Intermission
		g.SeenAt = s.SeenAt
		return g, true
	case "FINAL":
		ended := s.FinalAt
		if ended == 0 { // a row that was final before finalAt existed
			ended = max(s.SeenAt, s.AsOf)
		}
		if now.UnixMilli()-ended > finalFor.Milliseconds() {
			return Game{}, false
		}
		g.AwayScore, g.HomeScore = s.Away.Score, s.Home.Score
		g.Period = label(s.Period.Label)
		return g, true
	}
	return Game{}, false // OFF, or a state this does not know
}

// abbrev: two to four capital letters. Every NHL club is three; the
// allowance is for an all-star or international side, not for prose.
func abbrev(s string) bool {
	if len(s) < 2 || len(s) > 4 {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 'A' || s[i] > 'Z' {
			return false
		}
	}
	return true
}

func score(n int) bool { return n >= 0 && n <= 99 }

// label passes only the labels reduce.PeriodLabel can produce: a digit or
// two, optionally followed by OT, or OT or SO alone. Anything else is
// dropped from the row rather than dropping the row: the score is still true.
func label(s string) string {
	if len(s) == 0 || len(s) > 4 {
		return ""
	}
	i := 0
	for i < len(s) && s[i] >= '0' && s[i] <= '9' {
		i++
	}
	switch rest := s[i:]; {
	case i > 2:
		return ""
	case rest == "" && i > 0, rest == "OT", rest == "SO" && i == 0:
		return s
	}
	return ""
}
