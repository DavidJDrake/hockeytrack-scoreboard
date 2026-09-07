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
	// AsOf is only ever set by the clock heartbeat, because it is the only
	// fold that also re-anchors Clock.Seconds; a status event must leave it
	// alone (see TestOnlyClockHeartbeatSetsAsOf).
	if s.AsOf != 0 {
		t.Errorf("status event must not set asOf, got %d", s.AsOf)
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

// F1: only the clock heartbeat may set State.AsOf, because it is the only
// fold that also re-anchors Clock.Seconds. The device derives the live
// clock as Clock.Seconds - (now - AsOf); if a play event restamped AsOf
// without moving Clock.Seconds, the device would compute the clock from a
// fresh timestamp against a stale seconds value and everything on screen
// (the main clock and every penalty) would jump forward by the gap.
func TestOnlyClockHeartbeatSetsAsOf(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 700, "1551", t0))
	wantAsOf, wantSeconds := s.AsOf, s.Clock.Seconds
	if wantAsOf == 0 || wantSeconds != 700 {
		t.Fatalf("heartbeat did not anchor asOf/seconds: asOf=%d seconds=%d", wantAsOf, wantSeconds)
	}

	// 7 seconds of wall-clock time pass before a goal is folded.
	goal := Event{DetailType: "nhl.game.play", Time: t0.Add(7 * time.Second), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":300,"playType":"goal","homeTeam":"FLA","awayTeam":"CHI","scoringTeam":"FLA","period":1,"timeInPeriod":"07:00","score":{"CHI":0,"FLA":1},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"goal","details":{"scoringPlayerId":8473419,"eventOwnerTeamId":13}}}`)}
	s, changed, err := Reduce(s, goal)
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.AsOf != wantAsOf {
		t.Errorf("goal restamped asOf: before=%d after=%d (would jump the derived clock forward)", wantAsOf, s.AsOf)
	}
	if s.Clock.Seconds != wantSeconds {
		t.Errorf("goal moved clock.seconds: before=%d after=%d", wantSeconds, s.Clock.Seconds)
	}
}

// F2: EventBridge delivers at least once, so a status event can be
// redelivered after later, more current events have already been folded.
// The status fold must never let a stale redelivery move the game state
// backwards through PRE -> LIVE -> FINAL, and must never lower a score.
func TestStatusFoldNeverMovesGameBackwards(t *testing.T) {
	base := time.Date(2025, 10, 7, 22, 0, 0, 0, time.UTC)
	statusAt := func(gameState string, score map[string]int, at time.Time) Event {
		b, _ := json.Marshal(map[string]any{"schemaVersion": 1, "gameId": 2025020001, "gameState": gameState, "score": score})
		return Event{DetailType: "nhl.game.status", Detail: b, Time: at}
	}

	s, _, err := Reduce(State{}, statusAt("LIVE", map[string]int{"CHI": 3, "FLA": 1}, base))
	if err != nil || s.GameState != "LIVE" || s.Away.Score != 3 || s.Home.Score != 1 {
		t.Fatalf("setup: err=%v state=%+v", err, s)
	}

	// A stale redelivery reporting an earlier state and a lower score must
	// change nothing.
	stale := statusAt("PRE", map[string]int{"CHI": 1, "FLA": 0}, base.Add(time.Second))
	s2, changed, err := Reduce(s, stale)
	if err != nil {
		t.Fatal(err)
	}
	if changed || s2.GameState != "LIVE" || s2.Away.Score != 3 || s2.Home.Score != 1 {
		t.Errorf("stale status applied: changed=%v state=%+v", changed, s2)
	}

	// A stale redelivery at the *current* state but with an outdated score
	// (already superseded by a later goal) must not lower the score either.
	s3, _, err := Reduce(s, statusAt("LIVE", map[string]int{"CHI": 1, "FLA": 0}, base.Add(2*time.Second)))
	if err != nil {
		t.Fatal(err)
	}
	if s3.Away.Score != 3 || s3.Home.Score != 1 {
		t.Errorf("score regressed: %+v", s3)
	}

	// Once FINAL, a stale LIVE redelivery must never resurrect the game
	// (and, in particular, must not reopen the penalty box FINAL cleared).
	s4, _, err := Reduce(s, statusAt("FINAL", map[string]int{"CHI": 3, "FLA": 1}, base.Add(3*time.Second)))
	if err != nil || s4.GameState != "FINAL" {
		t.Fatalf("setup: err=%v state=%s", err, s4.GameState)
	}
	s5, changed2, err := Reduce(s4, statusAt("LIVE", map[string]int{"CHI": 3, "FLA": 1}, base.Add(4*time.Second)))
	if err != nil {
		t.Fatal(err)
	}
	if changed2 || s5.GameState != "FINAL" {
		t.Errorf("final regressed to live: changed=%v state=%s", changed2, s5.GameState)
	}
}

// F3: a goal scored before the roster arrives records jersey number 0
// (the reducer does not yet know the scoring player's number); once the
// roster arrives, it must backfill LastGoal.Number and report changed=true
// so the document republishes with the corrected number.
func TestRosterBackfillsLastGoalNumber(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if s.LastGoal == nil || s.LastGoal.Number != 0 {
		t.Fatalf("setup: lastGoal = %+v, want number 0 before the roster arrives", s.LastGoal)
	}
	s, changed, err := Reduce(s, load(t, "nhl.game.roster", "roster.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !changed {
		t.Error("backfilling a goal's number must report changed=true so the document republishes")
	}
	if s.LastGoal == nil || s.LastGoal.Number != 91 {
		t.Errorf("lastGoal = %+v, want number 91 backfilled from the roster", s.LastGoal)
	}
}

// The NHL's OFF state (game over, no more updates coming) must collapse to
// FINAL like the explicit FINAL state does.
func TestOffMapsToFinal(t *testing.T) {
	if got := mapGameState("OFF"); got != "FINAL" {
		t.Errorf("mapGameState(OFF) = %q, want FINAL", got)
	}
}

// The final event must clear the penalty box: it is the last update the
// panel will ever get for this game, so a still-boxed penalty would be
// stuck on screen forever.
func TestFinalEventClearsPenaltyBox(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "penalty_chi_min.json"))
	if len(s.Penalties) != 1 {
		t.Fatalf("setup: penalties = %+v", s.Penalties)
	}
	s, changed, err := Reduce(s, load(t, "nhl.game.final", "final.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.GameState != "FINAL" {
		t.Errorf("state = %s", s.GameState)
	}
	if len(s.Penalties) != 0 {
		t.Errorf("penalties should be cleared on final: %+v", s.Penalties)
	}
}
