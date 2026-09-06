package reduce

import (
	"encoding/json"
	"testing"
)

func TestParseSituation(t *testing.T) {
	cases := []struct{ code, pp, en string }{
		{"1551", "", ""},
		{"1451", "NYR", ""}, // away 4 skaters, home 5: home (NYR) on the PP
		{"1541", "TBL", ""},
		{"1441", "", ""},    // 4 on 4
		{"0651", "", "TBL"}, // away goalie pulled: 6 skaters, empty net TBL
		{"1560", "", "NYR"},
		{"", "", ""},
		{"abcd", "", ""},
	}
	for _, c := range cases {
		s := ParseSituation(c.code, "TBL", "NYR")
		if s.PP != c.pp || s.EmptyNet != c.en || s.Code != c.code {
			t.Errorf("%q: got pp=%q en=%q, want pp=%q en=%q", c.code, s.PP, s.EmptyNet, c.pp, c.en)
		}
	}
}

func TestPeriodLabel(t *testing.T) {
	for _, c := range []struct {
		n    int
		typ  string
		want string
	}{{1, "REG", "1"}, {3, "REG", "3"}, {4, "OT", "OT"}, {5, "OT", "2OT"}, {6, "OT", "3OT"}, {5, "SO", "SO"}, {0, "", ""}} {
		if got := PeriodLabel(c.n, c.typ); got != c.want {
			t.Errorf("PeriodLabel(%d,%q) = %q, want %q", c.n, c.typ, got, c.want)
		}
	}
}

func TestGameTime(t *testing.T) {
	if got := GameTime(1, 0, 300); got != 0 {
		t.Errorf("start = %d", got)
	}
	if got := GameTime(2, 418, 300); got != 1618 {
		t.Errorf("2nd period 6:58 = %d, want 1618", got)
	}
	if got := GameTime(4, 61, 300); got != 3661 {
		t.Errorf("regular-season OT 1:01 = %d, want 3661", got)
	}
	if got := GameTime(5, 0, 1200); got != 4800 {
		t.Errorf("playoff 2OT start = %d, want 4800", got)
	}
	if OTLength(2025020001) != 300 || OTLength(2025030112) != 1200 || OTLength(2026010001) != 300 {
		t.Error("OTLength must read the game-type digits of the id (02 regular, 03 playoffs)")
	}
	if PeriodLength(3, 1200) != 1200 || PeriodLength(4, 300) != 300 {
		t.Error("PeriodLength wrong")
	}
}

func TestStateJSONHidesInternals(t *testing.T) {
	s := State{V: 1, GameID: 1, GameState: "LIVE", LastSeq: 99, Roster: map[int64]int{8478010: 86}, OTLen: 300, Penalties: []Penalty{}}
	b, err := s.JSON()
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	json.Unmarshal(b, &m)
	for _, k := range []string{"LastSeq", "lastSeq", "Roster", "roster", "OTLen", "otLen", "ObservedAt", "Version"} {
		if _, ok := m[k]; ok {
			t.Errorf("internal field %q leaked into the document", k)
		}
	}
	if m["penalties"] == nil {
		t.Error("penalties must serialise as [] not null")
	}
	if _, ok := m["lastGoal"]; ok {
		t.Error("nil lastGoal must be omitted")
	}
}
