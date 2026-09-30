package panelconfig

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"hockeytrack-scoreboard/internal/settings"
)

func TestTheWholeDocumentEveryTime(t *testing.T) {
	lead := 120
	r, _ := settings.Resolve(settings.Settings{}, settings.Settings{CountdownLeadMin: &lead,
		Sleep: &settings.Sleep{Enabled: true, Start: "23:00", End: "07:00", Zone: "America/Toronto"}})
	got, err := Compose(2026020001, 1789871240471, r, nil, nil)
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
	got, _ := Compose(5, 9, settings.BuiltIn, nil, nil)
	if string(got[:11]) != `{"gameId":5` {
		t.Errorf("document starts %s", got[:11])
	}
}

// A panel reads gameId 0 as a game to select. A settings-only publish to a
// panel that follows nothing must say nothing about the game.
func TestAPanelFollowingNothingIsSentNullNotZero(t *testing.T) {
	got, _ := Compose(0, 0, settings.BuiltIn, nil, nil)
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
	got, _ := Compose(0, 0, r, nil, nil)
	want := `{"gameId":null,"display":{"v":1,"countdownLeadMin":720,"finalHoldMin":180,"rotate":270}}`
	if string(got) != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}

var nextGame = &Next{GameID: 2026020102, Away: "MTL", Home: "TOR", Start: "2026-10-15T02:00:00Z"}

// The next game is carried only when there is one, and left out rather than
// sent as null when there is not: a panel in the field reads the keys it
// knows, so a document without the key is the document it has always had.
func TestTheNextGameIsCarriedOnlyWhenThereIsOne(t *testing.T) {
	got, _ := Compose(2026020101, 7, settings.BuiltIn, nil, nextGame)
	want := `{"gameId":2026020101,"chosenAt":7,"display":{"v":1,"countdownLeadMin":720,"finalHoldMin":180},"next":{"gameId":2026020102,"away":"MTL","home":"TOR","start":"2026-10-15T02:00:00Z"}}`
	if string(got) != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
	got, _ = Compose(2026020101, 7, settings.BuiltIn, nil, nil)
	if strings.Contains(string(got), "next") {
		t.Errorf("no next game, yet the document says %s", got)
	}
}

// The panel drops a next it cannot read strictly, so one that would fail its
// reading is not written: the document says nothing rather than something
// the panel will not show.
func TestANextThePanelWouldDropIsNotWritten(t *testing.T) {
	bad := []Next{
		{GameID: 0, Away: "MTL", Home: "TOR", Start: "2026-10-15T02:00:00Z"},
		{GameID: -1, Away: "MTL", Home: "TOR", Start: "2026-10-15T02:00:00Z"},
		{GameID: 1, Away: "", Home: "TOR", Start: "2026-10-15T02:00:00Z"},
		{GameID: 1, Away: "mtl", Home: "TOR", Start: "2026-10-15T02:00:00Z"},
		{GameID: 1, Away: "MONTREAL", Home: "TOR", Start: "2026-10-15T02:00:00Z"},
		{GameID: 1, Away: "TOR", Home: "TOR", Start: "2026-10-15T02:00:00Z"},
		{GameID: 1, Away: "MTL", Home: "TOR", Start: ""},
		{GameID: 1, Away: "MTL", Home: "TOR", Start: "tonight"},
		{GameID: 1, Away: "MTL", Home: "TOR", Start: "2026-10-15 02:00"},
	}
	for _, n := range bad {
		n := n
		got, err := Compose(2026020101, 7, settings.BuiltIn, nil, &n)
		if err != nil || strings.Contains(string(got), "next") {
			t.Errorf("%+v was written: %s (err %v)", n, got, err)
		}
	}
	// A start with an offset is the season's form and passes.
	got, _ := Compose(2026020101, 7, settings.BuiltIn, nil, &Next{GameID: 1, Away: "MTL", Home: "TOR", Start: "2026-10-14T22:00:00-04:00"})
	if !strings.Contains(string(got), `"start":"2026-10-14T22:00:00-04:00"`) {
		t.Errorf("an offset start was not written: %s", got)
	}
}

// Adding next did not move gameId: the shape old panels rely on is checked
// with a next present too.
func TestGameIdStaysFirstWithANextGame(t *testing.T) {
	got, _ := Compose(5, 9, settings.BuiltIn, nil, nextGame)
	if string(got[:11]) != `{"gameId":5` {
		t.Errorf("document starts %s", got[:11])
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
				Next     *Next             `json:"next"`
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
		got, err := Compose(c.Compose.GameID, c.Compose.ChosenAt, r, c.Compose.Wake, c.Compose.Next)
		if err != nil || string(got) != c.Document {
			t.Errorf("%s:\n got  %s\n want %s (err %v)", c.Name, got, c.Document, err)
		}
	}
}
