package schedule

import (
	"encoding/json"
	"os"
	"reflect"
	"strconv"
	"testing"
	"time"
)

type vectors struct {
	OccupiesMinutes int `json:"occupiesMinutes"`
	Cases           []struct {
		Name  string `json:"name"`
		Games []struct {
			ID     int64  `json:"id"`
			Start  string `json:"start"`
			Source string `json:"source"`
		} `json:"games"`
		Order       []string  `json:"order"`
		Sequences   [][]int64 `json:"sequences"`
		DefaultKept []int64   `json:"defaultKept"`
		Unreadable  []int64   `json:"unreadable"`
	} `json:"cases"`
	Resolutions []struct {
		Name     string            `json:"name"`
		Sequence []int64           `json:"sequence"`
		Starts   map[string]string `json:"starts"`
		Kept     []int64           `json:"kept"`
		Valid    bool              `json:"valid"`
	} `json:"resolutions"`
}

func load(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile("../../../testdata/overlap-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil || len(v.Cases) == 0 {
		t.Fatalf("cases: %d, err %v", len(v.Cases), err)
	}
	return v
}

// testdata/overlap-vectors.json is run by this test and by
// site/tests/overlap.test.js. The site shows conflicts as boxes are ticked;
// this package gives the answer that counts. They must be the same answer.
func TestTheSharedCases(t *testing.T) {
	v := load(t)
	if time.Duration(v.OccupiesMinutes)*time.Minute != Occupies {
		t.Fatalf("the cases assume %d minutes; this package uses %v", v.OccupiesMinutes, Occupies)
	}
	for _, c := range v.Cases {
		games := make([]Game, len(c.Games))
		for i, g := range c.Games {
			games[i] = Game{g.ID, g.Start, g.Source}
		}
		got := Build(games, c.Order)
		if !reflect.DeepEqual(got.Sequences, c.Sequences) {
			t.Errorf("%s: sequences = %v, want %v", c.Name, got.Sequences, c.Sequences)
		}
		if !reflect.DeepEqual(got.DefaultKept, c.DefaultKept) {
			t.Errorf("%s: default kept = %v, want %v", c.Name, got.DefaultKept, c.DefaultKept)
		}
		if !reflect.DeepEqual(got.Unreadable, c.Unreadable) {
			t.Errorf("%s: unreadable = %v, want %v", c.Name, got.Unreadable, c.Unreadable)
		}
	}
	for _, r := range v.Resolutions {
		starts := map[int64]string{}
		for k, s := range r.Starts {
			id, _ := strconv.ParseInt(k, 10, 64)
			starts[id] = s
		}
		if got := ValidResolution(r.Sequence, r.Kept, starts); got != r.Valid {
			t.Errorf("%s: valid = %v, want %v", r.Name, got, r.Valid)
		}
	}
}

func TestTheDefaultNeverKeepsTwoGamesThatOverlap(t *testing.T) {
	// A property, over every shared case: whatever else is true, what a
	// panel is left showing must be showable.
	for _, c := range load(t).Cases {
		games := make([]Game, len(c.Games))
		starts := map[int64]time.Time{}
		for i, g := range c.Games {
			games[i] = Game{g.ID, g.Start, g.Source}
			if at, ok := parseStart(g.Start); ok {
				starts[g.ID] = at
			}
		}
		kept := Build(games, c.Order).DefaultKept
		for i := range kept {
			for j := i + 1; j < len(kept); j++ {
				if overlaps(starts[kept[i]], starts[kept[j]]) {
					t.Errorf("%s: the default keeps %d and %d, which overlap", c.Name, kept[i], kept[j])
				}
			}
		}
	}
}

func TestAnAnswerIsStoredAgainstTheQuestionItWasGivenFor(t *testing.T) {
	if Key([]int64{9, 4}) != "4-9" || Key([]int64{4, 9}) != "4-9" {
		t.Error("a sequence's key should not depend on the order it is listed in")
	}
	if Key([]int64{4, 9}) == Key([]int64{4, 9, 11}) {
		t.Error("a game joining a conflict must change its key, so the old answer stops applying")
	}
}

func TestBuildDoesNotChangeWhatItIsGiven(t *testing.T) {
	games := []Game{{9, "2026-10-10T23:30:00Z", "panel"}, {4, "2026-10-10T23:00:00Z", "panel"}}
	order := []string{"b", "a"}
	Build(games, order)
	if games[0].ID != 9 || order[0] != "b" {
		t.Error("Build reordered its caller's slices")
	}
}
