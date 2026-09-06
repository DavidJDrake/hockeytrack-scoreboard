package reduce

import (
	"encoding/json"
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
