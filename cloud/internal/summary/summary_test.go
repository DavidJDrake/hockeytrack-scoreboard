package summary

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"hockeytrack-scoreboard/internal/reduce"
)

var now = time.Date(2026, 9, 21, 23, 30, 0, 0, time.UTC)

func game(id int64, away, home, state string, mutate ...func(*reduce.State)) reduce.State {
	s := reduce.State{GameID: id, GameState: state, Start: "2026-09-21T23:00:00Z",
		Away: reduce.Team{Abbrev: away, Score: 2}, Home: reduce.Team{Abbrev: home, Score: 1},
		Period: reduce.Period{Number: 2, Type: "REG", Label: "2"}, SeenAt: now.UnixMilli() - 4000}
	for _, m := range mutate {
		m(&s)
	}
	return s
}

func TestALiveGameCarriesItsScoreAndWhereItIs(t *testing.T) {
	doc := Build([]reduce.State{game(2026010011, "TBL", "NYR", "LIVE", func(s *reduce.State) { s.Clock.Intermission = true })}, now)
	want := Game{GameID: 2026010011, Away: "TBL", Home: "NYR", AwayScore: 2, HomeScore: 1, State: "LIVE",
		Period: "2", Intermission: true, Start: "2026-09-21T23:00:00Z", SeenAt: now.UnixMilli() - 4000}
	if len(doc.Games) != 1 || doc.Games[0] != want {
		t.Fatalf("got %+v\nwant %+v", doc.Games, want)
	}
	if doc.V != V || doc.AsOf != now.UnixMilli() {
		t.Errorf("header: %+v", doc)
	}
}

func TestNoGamesIsAnEmptyListNotNull(t *testing.T) {
	// A panel indexes `games`. And an empty document has a job: it replaces
	// the retained one, which is how last night's scores come down.
	out, err := json.Marshal(Build(nil, now))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"games":[]`) {
		t.Errorf("got %s", out)
	}
}

func TestAGameNotStartedHasNoScoreAndIsOnlyListedNearItsStart(t *testing.T) {
	at := func(d time.Duration) func(*reduce.State) {
		return func(s *reduce.State) { s.Start = now.Add(d).Format(time.RFC3339) }
	}
	doc := Build([]reduce.State{
		game(1, "BOS", "MTL", "PRE", at(2*time.Hour)),
		game(2, "BOS", "MTL", "PRE", at(25*time.Hour)),    // tomorrow night
		game(3, "BOS", "MTL", "PRE", at(-4*time.Hour)),    // postponed, or never polled
		game(4, "BOS", "MTL", "PRE", at(-30*time.Minute)), // starting late
		game(5, "BOS", "MTL", "PRE", func(s *reduce.State) { s.Start = "soon" }),
	}, now)
	var ids []int64
	for _, g := range doc.Games {
		ids = append(ids, g.GameID)
		if g.AwayScore != 0 || g.HomeScore != 0 || g.Period != "" {
			t.Errorf("a game that has not started says %+v", g)
		}
	}
	if len(ids) != 2 || ids[0] != 4 || ids[1] != 1 {
		t.Errorf("listed %v, want [4 1]", ids)
	}
}

func TestAFinalIsTimedFromTheGamesEndAndComesDown(t *testing.T) {
	ended := func(ago time.Duration) func(*reduce.State) {
		return func(s *reduce.State) { s.FinalAt = now.Add(-ago).UnixMilli() }
	}
	doc := Build([]reduce.State{
		game(1, "UTA", "COL", "FINAL", ended(time.Hour)),
		game(2, "UTA", "COL", "FINAL", ended(7*time.Hour)),
		// No finalAt (a row from before it existed): the last time it was seen.
		game(3, "UTA", "COL", "FINAL", func(s *reduce.State) { s.SeenAt = now.Add(-2 * time.Hour).UnixMilli() }),
		game(4, "UTA", "COL", "FINAL", func(s *reduce.State) { s.SeenAt, s.AsOf = 0, now.Add(-8*time.Hour).UnixMilli() }),
	}, now)
	if len(doc.Games) != 2 || doc.Games[0].GameID != 1 || doc.Games[1].GameID != 3 {
		t.Fatalf("got %+v", doc.Games)
	}
	if doc.Games[0].SeenAt != 0 {
		t.Errorf("a final is not judged by freshness: %+v", doc.Games[0])
	}
}

func TestARowWithAnythingUnreadableInItIsLeftOutWhole(t *testing.T) {
	bad := map[string]func(*reduce.State){
		"no id":                 func(s *reduce.State) { s.GameID = 0 },
		"negative id":           func(s *reduce.State) { s.GameID = -5 },
		"no teams yet":          func(s *reduce.State) { s.Away.Abbrev = "" },
		"lower case":            func(s *reduce.State) { s.Home.Abbrev = "nyr" },
		"too long":              func(s *reduce.State) { s.Home.Abbrev = "RANGERS" },
		"markup":                func(s *reduce.State) { s.Home.Abbrev = "<b>" },
		"a null byte":           func(s *reduce.State) { s.Home.Abbrev = "NY\x00" },
		"not ASCII":             func(s *reduce.State) { s.Home.Abbrev = "NYÉ" },
		"a team playing itself": func(s *reduce.State) { s.Home.Abbrev = s.Away.Abbrev },
		"a negative score":      func(s *reduce.State) { s.Away.Score = -1 },
		"an absurd score":       func(s *reduce.State) { s.Home.Score = 100 },
		"off":                   func(s *reduce.State) { s.GameState = "OFF" },
		"an unknown state":      func(s *reduce.State) { s.GameState = "CRIT" },
		"no state":              func(s *reduce.State) { s.GameState = "" },
	}
	for name, mutate := range bad {
		if doc := Build([]reduce.State{game(7, "TBL", "NYR", "LIVE", mutate)}, now); len(doc.Games) != 0 {
			t.Errorf("%s: listed %+v", name, doc.Games)
		}
	}
}

func TestAPeriodLabelThatIsNotOneIsDroppedAndTheScoreKept(t *testing.T) {
	for label, want := range map[string]string{
		"1": "1", "3": "3", "OT": "OT", "2OT": "2OT", "10OT": "10OT", "SO": "SO",
		"": "", "2SO": "", "OTX": "", "<i>": "", "123": "", "2 OT": "", "ot": "", "2OT\n": "",
	} {
		doc := Build([]reduce.State{game(7, "TBL", "NYR", "LIVE", func(s *reduce.State) { s.Period.Label = label })}, now)
		if len(doc.Games) != 1 || doc.Games[0].Period != want || doc.Games[0].AwayScore != 2 {
			t.Errorf("label %q: got %+v, want period %q", label, doc.Games, want)
		}
	}
}

func TestEveryLabelTheReducerMakesPasses(t *testing.T) {
	for n := 1; n <= 9; n++ {
		for _, typ := range []string{"REG", "OT", "SO"} {
			if made := reduce.PeriodLabel(n, typ); label(made) != made {
				t.Errorf("PeriodLabel(%d, %s) = %q, which the summary drops", n, typ, made)
			}
		}
	}
}

func TestAStartIsWrittenInOneSpelling(t *testing.T) {
	doc := Build([]reduce.State{game(7, "TBL", "NYR", "LIVE", func(s *reduce.State) { s.Start = "2026-09-21T19:00:00-04:00" })}, now)
	if doc.Games[0].Start != "2026-09-21T23:00:00Z" {
		t.Errorf("got %q", doc.Games[0].Start)
	}
	doc = Build([]reduce.State{game(7, "TBL", "NYR", "LIVE", func(s *reduce.State) { s.Start = "<script>" })}, now)
	if len(doc.Games) != 1 || doc.Games[0].Start != "" {
		t.Errorf("a live game with an unreadable start keeps its score and loses the start: %+v", doc.Games)
	}
}

func TestGamesAreInStartOrderAndTheDocumentHasACeiling(t *testing.T) {
	var states []reduce.State
	for i := 0; i < MaxGames+10; i++ {
		id := int64(1000 - i)
		states = append(states, game(id, "TBL", "NYR", "LIVE", func(s *reduce.State) {
			s.Start = now.Add(-time.Duration(i%3) * time.Hour).Format(time.RFC3339)
		}))
	}
	doc := Build(states, now)
	if len(doc.Games) != MaxGames {
		t.Fatalf("got %d games, want the ceiling of %d", len(doc.Games), MaxGames)
	}
	for i := 1; i < len(doc.Games); i++ {
		a, b := doc.Games[i-1], doc.Games[i]
		if a.Start > b.Start || (a.Start == b.Start && a.GameID >= b.GameID) {
			t.Fatalf("out of order at %d: %+v then %+v", i, a, b)
		}
	}
	out, _ := json.Marshal(doc)
	if len(out) > 8<<10 {
		t.Errorf("a full document is %d bytes; a panel should never be sent more than 8 KB of this", len(out))
	}
}
