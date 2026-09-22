// Package panelconfig writes the one document a panel is configured by: the
// retained message on scoreboard/<thing>/config.
//
// One author. A retained message replaces the whole document, so every
// publish has to carry everything -- the game, its stamp, the settings -- or
// whatever was left out is gone from the panel's next reconnect. Anything
// that publishes to a panel builds the payload here, from typed values, so
// there is no code path that can send half a document and no way for a
// client's JSON to reach a panel.
package panelconfig

import (
	"encoding/json"

	"hockeytrack-scoreboard/internal/settings"
)

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
}

// wake is the owner's switch if one is in force, or nil. The caller decides
// "in force" (settings.Wake.Live): a switch that has ended is never sent.
func Compose(gameID, chosenAt int64, r settings.Resolved, wake *settings.Wake) ([]byte, error) {
	doc := document{ChosenAt: chosenAt, Display: r.Wire()}
	doc.Display.Wake = wake
	if gameID != 0 {
		doc.GameID = &gameID
	}
	return json.Marshal(doc)
}

func Topic(thingName string) string { return "scoreboard/" + thingName + "/config" }
