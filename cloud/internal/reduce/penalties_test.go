package reduce

import (
	"encoding/json"
	"testing"
	"time"
)

func clockAt(period, remaining int, code string, obs time.Time) Event {
	b, _ := json.Marshal(map[string]any{"schemaVersion": 1, "gameId": 2025020001, "gameState": "LIVE", "period": period, "periodType": "REG",
		"secondsRemaining": remaining, "timeRemaining": "x", "running": true, "inIntermission": false, "situationCode": code,
		"homeTeam": "FLA", "awayTeam": "CHI", "score": map[string]int{"CHI": 0, "FLA": 0}, "shots": map[string]int{"CHI": 1, "FLA": 1}, "observedAt": obs})
	return Event{DetailType: "nhl.game.clock", Detail: b, Time: obs}
}

func liveWithRoster(t *testing.T) State {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.roster", "roster.json"))
	return s
}

func TestMinorPenaltyIsBoxedWithNumberAndTicksDown(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0))                               // 13:02 left when the penalty is called
	s, changed, err := Reduce(s, load(t, "nhl.game.play", "penalty_chi_min.json")) // 06:58 elapsed in P1
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if len(s.Penalties) != 1 {
		t.Fatalf("penalties = %+v", s.Penalties)
	}
	p := s.Penalties[0]
	if p.Team != "CHI" || p.Number != 81 || p.Type != "MIN" || p.Seconds != 120 || !p.EndsOnGoal {
		t.Errorf("penalty = %+v", p)
	}
	// 45 game-seconds later (12:17 left in P1) the clock heartbeat ticks it.
	s, _, _ = Reduce(s, clockAt(1, 737, "1451", t0.Add(45*time.Second)))
	if s.Penalties[0].Seconds != 75 {
		t.Errorf("remaining = %d, want 75", s.Penalties[0].Seconds)
	}
	if s.Situation.PP != "FLA" {
		t.Errorf("pp = %q", s.Situation.PP)
	}
	// Expiry: 2:00 after 06:58 is 08:58 elapsed → 11:02 remaining.
	s, _, _ = Reduce(s, clockAt(1, 662, "1551", t0.Add(120*time.Second)))
	if len(s.Penalties) != 0 {
		t.Errorf("penalty should have expired: %+v", s.Penalties)
	}
}

func TestPenaltySpansIntermission(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 40, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 30, "1551", t0))
	pen := load(t, "nhl.game.play", "penalty_chi_min.json")
	// Rewrite the fixture's time to 19:30 elapsed in P1.
	var m map[string]any
	json.Unmarshal(pen.Detail, &m)
	m["timeInPeriod"] = "19:30"
	m["seq"] = float64(900)
	pen.Detail, _ = json.Marshal(m)
	s, _, _ = Reduce(s, pen)
	// First heartbeat of P2 at 19:00 remaining: 30 s of P1 + 60 s of P2 = 90 s served.
	s, _, _ = Reduce(s, clockAt(2, 1140, "1451", t0.Add(20*time.Minute)))
	if len(s.Penalties) != 1 || s.Penalties[0].Seconds != 30 {
		t.Errorf("penalties = %+v, want one with 30 s left", s.Penalties)
	}
}

func TestPowerPlayGoalEndsOldestMinor(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "penalty_chi_min.json"))
	s, _, _ = Reduce(s, clockAt(1, 760, "1451", t0.Add(22*time.Second))) // FLA on the PP
	// FLA scores.
	goal := Event{DetailType: "nhl.game.play", Time: t0.Add(30 * time.Second), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":300,"playType":"goal","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"FLA","scoringTeam":"FLA","period":1,"timeInPeriod":"07:30","score":{"CHI":0,"FLA":1},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"goal","details":{"scoringPlayerId":8473419,"eventOwnerTeamId":13}}}`)}
	s, _, _ = Reduce(s, goal)
	if len(s.Penalties) != 0 {
		t.Errorf("minor should end on the PP goal: %+v", s.Penalties)
	}
	if s.LastGoal == nil || s.LastGoal.Number != 63 {
		t.Errorf("lastGoal = %+v", s.LastGoal)
	}
}

func TestMajorSurvivesGoalAndBenchMinorUsesServer(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0))
	major := Event{DetailType: "nhl.game.play", Time: t0, Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":126,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"penalty","details":{"typeCode":"MAJ","duration":5,"committedByPlayerId":8483493,"eventOwnerTeamId":16}}}`)}
	bench := Event{DetailType: "nhl.game.play", Time: t0, Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":127,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"penalty","details":{"typeCode":"BEN","duration":2,"servedByPlayerId":8484783,"eventOwnerTeamId":16}}}`)}
	ps := Event{DetailType: "nhl.game.play", Time: t0, Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":128,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"penalty","details":{"typeCode":"PS","duration":0,"committedByPlayerId":8483493,"eventOwnerTeamId":16}}}`)}
	for _, e := range []Event{major, bench, ps} {
		s, _, _ = Reduce(s, e)
	}
	if len(s.Penalties) != 2 {
		t.Fatalf("penalties = %+v, want MAJ + BEN only", s.Penalties)
	}
	if s.Penalties[0].Type != "MAJ" || s.Penalties[0].Seconds != 300 || s.Penalties[0].EndsOnGoal || s.Penalties[0].Number != 91 {
		t.Errorf("major = %+v", s.Penalties[0])
	}
	if s.Penalties[1].Type != "BEN" || s.Penalties[1].Number != 81 || !s.Penalties[1].EndsOnGoal {
		t.Errorf("bench minor = %+v", s.Penalties[1])
	}
	s, _, _ = Reduce(s, clockAt(1, 770, "1351", t0.Add(12*time.Second)))
	goal := Event{DetailType: "nhl.game.play", Time: t0.Add(15 * time.Second), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":300,"playType":"goal","homeTeam":"FLA","awayTeam":"CHI","scoringTeam":"FLA","period":1,"timeInPeriod":"07:13","score":{"CHI":0,"FLA":1},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"goal","details":{"scoringPlayerId":8473419,"eventOwnerTeamId":13}}}`)}
	s, _, _ = Reduce(s, goal)
	if len(s.Penalties) != 1 || s.Penalties[0].Type != "MAJ" {
		t.Errorf("after PP goal want only the major left: %+v", s.Penalties)
	}
}
