package reduce

import (
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"
)

func load(t *testing.T, detailType, file string) Event {
	t.Helper()
	b, err := os.ReadFile("testdata/events/" + file)
	if err != nil {
		t.Fatal(err)
	}
	return Event{DetailType: detailType, Detail: json.RawMessage(b), Time: time.Date(2025, 10, 7, 22, 31, 6, 0, time.UTC)}
}

func TestStatusEventStartsAGame(t *testing.T) {
	s, changed, err := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.V != 1 || s.GameID != 2025020001 || s.GameState != "LIVE" {
		t.Errorf("state = %+v", s)
	}
	if s.Away.Abbrev != "CHI" || s.Home.Abbrev != "FLA" {
		t.Errorf("teams from score map: away=%q home=%q", s.Away.Abbrev, s.Home.Abbrev)
	}
	if s.AsOf == 0 {
		t.Error("asOf must be set from the event time")
	}
}

func TestClockHeartbeatUpdatesEverything(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, changed, err := Reduce(s, load(t, "nhl.game.clock", "clock_p2.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.Period.Number != 2 || s.Period.Label != "2" || s.Clock.Seconds != 872 || !s.Clock.Running || s.Clock.Intermission {
		t.Errorf("period/clock = %+v / %+v", s.Period, s.Clock)
	}
	if s.Away.Score != 2 || s.Home.Score != 1 || s.Away.SOG != 17 || s.Home.SOG != 22 {
		t.Errorf("score/sog = %+v / %+v", s.Away, s.Home)
	}
	if s.Situation.PP != "FLA" || s.Situation.Code != "1451" {
		t.Errorf("situation = %+v", s.Situation)
	}
	if s.Away.Color == "" || s.Home.Color == "" {
		t.Error("team colours should be filled from the table")
	}
	if s.OTLen != 300 {
		t.Errorf("otLen = %d, want 300 for a regular-season id", s.OTLen)
	}
	if s.AsOf != time.Date(2025, 10, 7, 22, 31, 6, 0, time.UTC).UnixMilli() {
		t.Errorf("asOf = %d", s.AsOf)
	}
}

func TestStaleClockIsIgnored(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.clock", "clock_p2.json"))
	older := load(t, "nhl.game.clock", "clock_intermission.json") // observedAt 21:52 < 22:31
	s2, changed, err := Reduce(s, older)
	if err != nil {
		t.Fatal(err)
	}
	if changed || s2.Period.Number != 2 {
		t.Errorf("stale heartbeat applied: changed=%v period=%d", changed, s2.Period.Number)
	}
}

func TestIntermissionClock(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.clock", "clock_intermission.json"))
	if !s.Clock.Intermission || s.Clock.Running || s.Clock.Seconds != 0 || s.Period.Number != 1 {
		t.Errorf("intermission state = %+v / %+v", s.Clock, s.Period)
	}
}

func TestUnknownDetailTypeIsIgnored(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s2, changed, err := Reduce(s, Event{DetailType: "hockeytrack.alert", Detail: json.RawMessage(`{"gameId":2025020001,"reason":"x"}`)})
	if err != nil || changed || s2.GameState != "LIVE" {
		t.Errorf("err=%v changed=%v state=%+v", err, changed, s2)
	}
}

// clockEvent builds a heartbeat inline so this test does not depend on a
// helper a later task introduces.
func clockEvent(period int, periodType, code string, at time.Time) Event {
	body := fmt.Sprintf(`{"gameId":2025020001,"gameState":"LIVE","period":%d,"periodType":%q,`+
		`"secondsRemaining":0,"running":false,"inIntermission":false,"situationCode":%q,`+
		`"homeTeam":"FLA","awayTeam":"CHI","score":{"CHI":2,"FLA":2},"shots":{"CHI":30,"FLA":31},`+
		`"observedAt":%q}`, period, periodType, code, at.Format(time.RFC3339))
	return Event{DetailType: "nhl.game.clock", Detail: json.RawMessage(body), Time: at}
}

func TestGoalUpdatesScoreAndLastGoalWithNumber(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.roster", "roster.json"))
	s, changed, err := Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.Away.Abbrev != "CHI" || s.Home.Abbrev != "FLA" {
		t.Errorf("play event must fix home/away: %+v %+v", s.Away, s.Home)
	}
	if s.Away.Score != 1 || s.Home.Score != 0 {
		t.Errorf("score = %d-%d", s.Away.Score, s.Home.Score)
	}
	if s.LastGoal == nil || s.LastGoal.Team != "CHI" || s.LastGoal.Number != 91 || s.LastGoal.AsOf == 0 {
		t.Errorf("lastGoal = %+v", s.LastGoal)
	}
	if s.LastSeq != 166 {
		t.Errorf("lastSeq = %d", s.LastSeq)
	}
}

func TestDuplicatePlayIsIgnored(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	s2, changed, _ := Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if changed || s2.Away.Score != 1 {
		t.Errorf("duplicate applied: changed=%v score=%d", changed, s2.Away.Score)
	}
}

func TestGoalWithoutRosterHasNumberZero(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if s.LastGoal == nil || s.LastGoal.Number != 0 {
		t.Errorf("lastGoal = %+v, want number 0 when the roster is unknown", s.LastGoal)
	}
}

func TestPeriodStartLearnsPeriodAndClearsIntermission(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.clock", "clock_intermission.json"))
	ps := Event{DetailType: "nhl.game.play", Time: time.Now(), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":400,"playType":"period-start","homeTeam":"FLA","awayTeam":"CHI","period":2,"timeInPeriod":"00:00","score":{"CHI":1,"FLA":0},"raw":{"periodDescriptor":{"number":2,"periodType":"REG"},"typeDescKey":"period-start","details":{}}}`)}
	s, changed, err := Reduce(s, ps)
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.Period.Number != 2 || s.Period.Label != "2" || s.Clock.Intermission {
		t.Errorf("after period-start: %+v %+v", s.Period, s.Clock)
	}
}

// A shootout's situation codes (0101, 1010) describe one skater against a
// goalie with the other net empty, so they parse as a real empty net. Neither
// the power-play nor the empty-net indicator means anything there, and an EN
// badge would sit on screen for the whole shootout.
func TestShootoutClearsPowerPlayAndEmptyNet(t *testing.T) {
	base := time.Date(2025, 10, 7, 23, 0, 0, 0, time.UTC)
	for i, code := range []string{"0101", "1010"} {
		var s State
		s, _, _ = Reduce(s, load(t, "nhl.game.status", "status_live.json"))
		s, _, err := Reduce(s, clockEvent(5, "SO", code, base.Add(time.Duration(i)*time.Minute)))
		if err != nil {
			t.Fatal(err)
		}
		if s.Situation.PP != "" || s.Situation.EmptyNet != "" {
			t.Errorf("code %s in a shootout: pp=%q emptyNet=%q, want both empty", code, s.Situation.PP, s.Situation.EmptyNet)
		}
		if s.Situation.Code != code {
			t.Errorf("code %s should still be carried through, got %q", code, s.Situation.Code)
		}
	}
	// The same shape of code outside a shootout must still report the empty net.
	var s State
	s, _, _ = Reduce(s, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, clockEvent(3, "REG", "1560", base.Add(5*time.Minute)))
	if s.Situation.EmptyNet == "" {
		t.Error("a pulled goalie in regulation must still report an empty net")
	}
}
