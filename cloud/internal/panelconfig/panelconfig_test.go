package panelconfig

import (
	"encoding/json"
	"os"
	"testing"

	"hockeytrack-scoreboard/internal/settings"
)

func TestTheWholeDocumentEveryTime(t *testing.T) {
	lead := 120
	r, _ := settings.Resolve(settings.Settings{}, settings.Settings{CountdownLeadMin: &lead,
		Sleep: &settings.Sleep{Enabled: true, Start: "23:00", End: "07:00", Zone: "America/Toronto"}})
	got, err := Compose(2026020001, 1789871240471, r, nil)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"gameId":2026020001,"chosenAt":1789871240471,"display":{"v":1,"countdownLeadMin":120,"finalHoldMin":180,"sleep":{"start":"23:00","end":"07:00","zone":"America/Toronto"}}}`
	if string(got) != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}

// Panels in the field read gameId and ignore the rest. It must stay a
// top-level number, first in the document.
func TestGameIdStaysWhereOldPanelsLookForIt(t *testing.T) {
	got, _ := Compose(5, 9, settings.BuiltIn, nil)
	if string(got[:11]) != `{"gameId":5` {
		t.Errorf("document starts %s", got[:11])
	}
}

// A panel reads gameId 0 as a game to select. A settings-only publish to a
// panel that follows nothing must say nothing about the game.
func TestAPanelFollowingNothingIsSentNullNotZero(t *testing.T) {
	got, _ := Compose(0, 0, settings.BuiltIn, nil)
	want := `{"gameId":null,"display":{"v":1,"countdownLeadMin":720,"finalHoldMin":180}}`
	if string(got) != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}

// The panel turns every frame through the value in the document, so it is
// carried only when the owner set one: a panel nobody asked to turn keeps
// deciding from its own shape and its own card.
func TestOrientationIsCarriedOnlyWhenSet(t *testing.T) {
	rot := settings.Rotate(270)
	r, _ := settings.Resolve(settings.Settings{}, settings.Settings{Rotate: &rot})
	got, _ := Compose(0, 0, r, nil)
	want := `{"gameId":null,"display":{"v":1,"countdownLeadMin":720,"finalHoldMin":180,"rotate":270}}`
	if string(got) != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}

func TestTopic(t *testing.T) {
	if Topic("scoreboard-7qf2") != "scoreboard/scoreboard-7qf2/config" {
		t.Error(Topic("scoreboard-7qf2"))
	}
}

// testdata/config-documents.json is read by this test and by the device
// suite. Go writes the document; the panel's own parsers read it.
func TestTheSharedDocumentsAreWhatThisPackageWrites(t *testing.T) {
	raw, err := os.ReadFile("../../../testdata/config-documents.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []struct {
			Name    string `json:"name"`
			Compose struct {
				GameID   int64             `json:"gameId"`
				ChosenAt int64             `json:"chosenAt"`
				Account  settings.Settings `json:"account"`
				Panel    settings.Settings `json:"panel"`
				Wake     *settings.Wake    `json:"wake"`
			} `json:"compose"`
			Document string `json:"document"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil || len(file.Cases) == 0 {
		t.Fatalf("cases: %d, err %v", len(file.Cases), err)
	}
	for _, c := range file.Cases {
		account, err1 := settings.Normalize(c.Compose.Account)
		panel, err2 := settings.Normalize(c.Compose.Panel)
		if err1 != nil || err2 != nil {
			t.Fatalf("%s: settings do not validate: %v %v", c.Name, err1, err2)
		}
		r, _ := settings.Resolve(account, panel)
		got, err := Compose(c.Compose.GameID, c.Compose.ChosenAt, r, c.Compose.Wake)
		if err != nil || string(got) != c.Document {
			t.Errorf("%s:\n got  %s\n want %s (err %v)", c.Name, got, c.Document, err)
		}
	}
}
