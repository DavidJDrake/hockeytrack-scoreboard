package main

import (
	"encoding/json"
	"testing"
)

func TestTheEventNamesAPanelOrTheWholeFleet(t *testing.T) {
	for payload, want := range map[string]string{
		`{"thing":"scoreboard-01"}`:       "scoreboard-01", // the API, after a save
		`{}`:                              "",              // the scheduler
		``:                                "",
		`null`:                            "",
		`"scoreboard-01"`:                 "", // not the shape agreed; a sweep, not a guess
		`{"thing":"scoreboard-01","x":1}`: "scoreboard-01",
		`{"thing":["scoreboard-01"]}`:     "",
		`{"Thing":"scoreboard-01"}`:       "scoreboard-01", // encoding/json matches case-insensitively
		`not json`:                        "",
	} {
		if got := thingIn(json.RawMessage(payload)); got != want {
			t.Errorf("%s: got %q, want %q", payload, got, want)
		}
	}
}
