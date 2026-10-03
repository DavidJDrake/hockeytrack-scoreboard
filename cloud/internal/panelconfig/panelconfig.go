// Package panelconfig writes the one document a panel is configured by: the
// retained message on scoreboard/<thing>/config.
//
// One author. A retained message replaces the whole document, so every
// publish has to carry everything -- the game, its stamp, the settings, the
// next game -- or whatever was left out is gone from the panel's next
// reconnect. Anything that publishes to a panel builds the payload here, from
// typed values, so there is no code path that can send half a document and no
// way for a client's JSON to reach a panel.
//
// The next game (SCO-56) is the first addition that is not always there. It
// is omitted, never null, when there is none: a panel in the field reads the
// keys it knows and ignores the rest, so a document without the key is the
// document it has always had, and "no key" and "no next game" are the same
// state on every panel, old or new. It is also checked here before it is
// written, though the director builds it from a season that already checked
// the same things: the panel drops a next it cannot read strictly (any field
// wrong, the whole slot goes), so a document that carried one would only
// disagree with what the panel shows. Better that the document say nothing.
package panelconfig

import (
	"encoding/json"
	"time"

	"hockeytrack-scoreboard/internal/settings"
)

// Next is the panel's next kept game, the strip's "up next" (drawn by
// SCO-57, not here). Start is RFC 3339 in UTC, as the season normalizes it
// (Compose also passes an offset form, but the season never writes one);
// Away and Home are the league's abbreviations.
type Next struct {
	GameID int64  `json:"gameId"`
	Away   string `json:"away"`
	Home   string `json:"home"`
	Start  string `json:"start"`
}

// valid is the panel's reading of a next, applied before it is written: the
// same bounds the season holds a game to (internal/season), so a next that
// fails here is one that never came from the season.
func (n Next) valid() bool {
	if n.GameID <= 0 || !abbrev(n.Away) || !abbrev(n.Home) || n.Away == n.Home {
		return false
	}
	_, err := time.Parse(time.RFC3339, n.Start)
	return err == nil
}

// abbrev is two to four capital ASCII letters, the season's own rule.
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

type document struct {
	// gameId stays a top-level number and stays first: panels in the field
	// (v0.1.3 on) read that key and ignore the rest, so adding to this
	// document must never move or rename it. null when the panel follows
	// nothing: a panel reads 0 as a game to select, and null as "no news
	// about the game", which is what a settings-only publish to such a panel
	// is.
	GameID *int64 `json:"gameId"`
	// chosenAt is when the owner last chose the game. It is re-sent
	// unchanged by every publish that is not a choice, because a panel reads
	// a stamp it has not seen as the owner pressing a button.
	ChosenAt int64         `json:"chosenAt,omitempty"`
	Display  settings.Wire `json:"display"`
	// next is the game after this one, when the director knows it. Omitted
	// rather than null when there is none: see the package comment.
	Next *Next `json:"next,omitempty"`
}

// wake is the owner's switch if one is in force, or nil. The caller decides
// "in force" (settings.Wake.Live): a switch that has ended is never sent.
//
// next is the panel's next kept game, or nil for none, which leaves the key
// out. A next that would not pass the panel's own reading is left out too.
func Compose(gameID, chosenAt int64, r settings.Resolved, wake *settings.Wake, next *Next) ([]byte, error) {
	doc := document{ChosenAt: chosenAt, Display: r.Wire()}
	doc.Display.Wake = wake
	if gameID != 0 {
		doc.GameID = &gameID
	}
	if next != nil && next.valid() {
		n := *next
		doc.Next = &n
	}
	return json.Marshal(doc)
}

func Topic(thingName string) string { return "scoreboard/" + thingName + "/config" }
