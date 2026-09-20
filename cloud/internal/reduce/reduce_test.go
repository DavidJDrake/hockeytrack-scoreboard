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

func runningClock(seconds, period int, running bool, at time.Time) Event {
	body := fmt.Sprintf(`{"gameId":2025020001,"gameState":"LIVE","period":%d,"periodType":"REG",`+
		`"secondsRemaining":%d,"running":%t,"inIntermission":false,"situationCode":"1551",`+
		`"homeTeam":"FLA","awayTeam":"CHI","score":{"CHI":0,"FLA":2},"shots":{"CHI":3,"FLA":4},`+
		`"observedAt":%q}`, period, seconds, running, at.Format(time.RFC3339))
	return Event{DetailType: "nhl.game.clock", Detail: json.RawMessage(body), Time: at}
}

// Seen on the first live game on a real panel (VAN at SEA, 2026-09-19): the
// clock counted down five seconds, jumped back, and did it again, for as long
// as play ran. The NHL's feed repeats one clock value for 20 to 40 seconds
// while saying "running"; we poll every five. Each repeat was stamped as if
// it had been read off the scoreboard that instant, so the panel's
// seconds - (now - asOf) started over from the same number every time.
func TestARepeatedRunningSampleKeepsItsAnchor(t *testing.T) {
	t0 := time.Date(2026, 9, 20, 2, 29, 27, 0, time.UTC)
	s, _, _ := Reduce(State{}, runningClock(369, 2, true, t0))
	if s.AsOf != t0.UnixMilli() {
		t.Fatalf("first sample: asOf = %d, want %d", s.AsOf, t0.UnixMilli())
	}
	for i := 1; i <= 4; i++ {
		at := t0.Add(time.Duration(i) * 5 * time.Second)
		var changed bool
		s, changed, _ = Reduce(s, runningClock(369, 2, true, at))
		if s.AsOf != t0.UnixMilli() {
			t.Fatalf("repeat %d: asOf moved to %d; the panel's clock would jump back", i, s.AsOf)
		}
		// The panel judges freshness by whether the document changed. A
		// repeat must still change it, or a healthy feed reads as NO UPDATES.
		if !changed || s.SeenAt != at.UnixMilli() {
			t.Fatalf("repeat %d: changed=%v seenAt=%d, want a document that says it was just confirmed", i, changed, s.SeenAt)
		}
	}
	// The feed catches up: a new value is a new reading, anchored to now.
	t1 := t0.Add(40 * time.Second)
	s, _, _ = Reduce(s, runningClock(326, 2, true, t1))
	if s.AsOf != t1.UnixMilli() || s.Clock.Seconds != 326 {
		t.Errorf("new sample: asOf=%d seconds=%d", s.AsOf, s.Clock.Seconds)
	}
}

func TestOnlyAGenuineRepeatKeepsTheAnchor(t *testing.T) {
	t0 := time.Date(2026, 9, 20, 2, 29, 27, 0, time.UTC)
	at := t0.Add(5 * time.Second)
	cases := []struct {
		name        string
		first, next Event
	}{
		{"the clock was stopped", runningClock(369, 2, false, t0), runningClock(369, 2, true, at)},
		{"the clock has stopped", runningClock(369, 2, true, t0), runningClock(369, 2, false, at)},
		{"a stopped clock, repeated", runningClock(369, 2, false, t0), runningClock(369, 2, false, at)},
		{"another period", runningClock(1200, 2, true, t0), runningClock(1200, 3, true, at)},
		{"a different value", runningClock(369, 2, true, t0), runningClock(368, 2, true, at)},
	}
	for _, c := range cases {
		s, _, _ := Reduce(State{}, c.first)
		s, _, _ = Reduce(s, c.next)
		if s.AsOf != at.UnixMilli() {
			t.Errorf("%s: asOf = %d, want re-anchored to %d", c.name, s.AsOf, at.UnixMilli())
		}
	}
}

// If the same value keeps coming for a whole minute, the likelier story is
// that play stopped and the feed has not said so. Extrapolating on is then
// the bigger lie; go back to trusting the sample.
func TestARepeatIsNotTrustedForEver(t *testing.T) {
	t0 := time.Date(2026, 9, 20, 2, 29, 27, 0, time.UTC)
	s, _, _ := Reduce(State{}, runningClock(369, 2, true, t0))
	var at time.Time
	for i := 1; i <= 12; i++ {
		at = t0.Add(time.Duration(i) * 5 * time.Second)
		s, _, _ = Reduce(s, runningClock(369, 2, true, at))
	}
	if s.AsOf != at.UnixMilli() {
		t.Errorf("after %v of one value, asOf = %d, want re-anchored to %d", at.Sub(t0), s.AsOf, at.UnixMilli())
	}
}

// folded reports whether a play is in the state, which is not what `changed`
// says: the handler saves the state after every event, and `changed` only
// decides whether a new document is worth publishing. A faceoff folds and
// changes nothing anybody can see.
func folded(s State, eventID int64) bool {
	for _, id := range s.SeenEvents {
		if id == eventID {
			return true
		}
	}
	return false
}

func playEvent(eventID, seq int64, playType string, score map[string]int, at time.Time) Event {
	body, _ := json.Marshal(map[string]any{
		"gameId": 2025020001, "eventId": eventID, "seq": seq, "playType": playType,
		"homeTeam": "FLA", "awayTeam": "CHI", "scoringTeam": "", "period": 2, "timeInPeriod": "01:00",
		"score": score, "raw": map[string]any{"details": map[string]any{}},
	})
	if eventID == 0 { // a publisher from before eventId existed
		var m map[string]any
		_ = json.Unmarshal(body, &m)
		delete(m, "eventId")
		body, _ = json.Marshal(m)
	}
	return Event{DetailType: "nhl.game.play", Detail: json.RawMessage(body), Time: at}
}

// Found 2026-09-20 from a panel missing a penalty. The NHL gives the plays
// that open a period provisional sort orders in the 9000s and renumbers them
// a minute later. This reducer kept the highest seq it had folded and dropped
// anything at or below it, so one such play meant every later play of the
// game was dropped as a duplicate. HockeyTrack's poller had the same fault
// and is fixed the same way: identity is the eventId, which does not change.
func TestOneHighSeqDoesNotSilenceTheRestOfTheGame(t *testing.T) {
	at := time.Date(2026, 9, 20, 1, 0, 3, 0, time.UTC)
	s, _, _ := Reduce(State{}, playEvent(19, 276, "period-end", map[string]int{"CHI": 0, "FLA": 1}, at))
	s, _, _ = Reduce(s, playEvent(385, 9004, "faceoff", map[string]int{"CHI": 0, "FLA": 1}, at))
	if !folded(s, 385) {
		t.Fatal("the provisional play was not folded at all")
	}
	if s.LastSeq >= provisionalSeq {
		t.Errorf("lastSeq = %d: a provisional number was recorded", s.LastSeq)
	}
	s, _, _ = Reduce(s, playEvent(24, 285, "stoppage", map[string]int{"CHI": 0, "FLA": 2}, at))
	if !folded(s, 24) || s.Home.Score != 2 {
		t.Fatalf("a play after a 9004 was dropped: folded=%v score=%d", folded(s, 24), s.Home.Score)
	}
	// And the kind that matters: a penalty after it reaches the panel.
	_, changed, _ := Reduce(s, playEvent(25, 290, "penalty", map[string]int{"CHI": 0, "FLA": 2}, at))
	if !changed {
		t.Error("a penalty after a 9004 was not published")
	}
}

func TestAPlayDeliveredTwiceIsFoldedOnce(t *testing.T) {
	at := time.Date(2026, 9, 20, 1, 0, 3, 0, time.UTC)
	s, _, _ := Reduce(State{}, playEvent(24, 285, "stoppage", map[string]int{"CHI": 0, "FLA": 1}, at))
	s, _, _ = Reduce(s, playEvent(40, 300, "penalty", map[string]int{"CHI": 0, "FLA": 1}, at))
	pens := len(s.Penalties)
	again, changed, _ := Reduce(s, playEvent(40, 300, "penalty", map[string]int{"CHI": 0, "FLA": 1}, at))
	if changed || len(again.Penalties) != pens {
		t.Errorf("the same penalty was folded twice: %d then %d", pens, len(again.Penalties))
	}
	// The same play after the NHL renumbered it is still the same play.
	again, changed, _ = Reduce(s, playEvent(40, 307, "penalty", map[string]int{"CHI": 0, "FLA": 1}, at))
	if changed || len(again.Penalties) != pens || len(again.SeenEvents) != len(s.SeenEvents) {
		t.Error("a renumbered play was folded again")
	}
}

// Now that a play arriving late is folded rather than dropped, it must not
// drag the score back to what it was when the play happened.
func TestALatePlayDoesNotLowerTheScore(t *testing.T) {
	at := time.Date(2026, 9, 20, 1, 0, 3, 0, time.UTC)
	s, _, _ := Reduce(State{}, playEvent(30, 300, "stoppage", map[string]int{"CHI": 1, "FLA": 3}, at))
	s, _, _ = Reduce(s, playEvent(29, 275, "faceoff", map[string]int{"CHI": 0, "FLA": 2}, at))
	if !folded(s, 29) {
		t.Error("a play inserted late, below the highest seq seen, was dropped")
	}
	if s.Away.Score != 1 || s.Home.Score != 3 {
		t.Errorf("score = %d-%d, want it left at 1-3", s.Away.Score, s.Home.Score)
	}
}

func TestAPublisherWithoutEventIdsIsHandledAsBefore(t *testing.T) {
	at := time.Date(2026, 9, 20, 1, 0, 3, 0, time.UTC)
	s, _, _ := Reduce(State{}, playEvent(0, 285, "stoppage", map[string]int{"CHI": 0, "FLA": 1}, at))
	if got, _, _ := Reduce(s, playEvent(0, 285, "stoppage", map[string]int{"CHI": 0, "FLA": 5}, at)); got.Home.Score != 1 {
		t.Error("without eventIds, a repeated seq should still be dropped")
	}
	if got, _, _ := Reduce(s, playEvent(0, 286, "faceoff", map[string]int{"CHI": 0, "FLA": 2}, at)); got.Home.Score != 2 || got.LastSeq != 286 {
		t.Error("without eventIds, a higher seq should still be folded")
	}
}

func TestAGameAlreadyPoisonedRecoversOnItsNextPlay(t *testing.T) {
	// The stored state of four live games on 2026-09-20: lastSeq in the
	// 9000s, no list of events.
	s := State{GameID: 2025020001, LastSeq: 9006}
	s.setTeams("CHI", "FLA")
	s, changed, _ := Reduce(s, playEvent(999, 640, "penalty", map[string]int{"CHI": 0, "FLA": 1}, time.Date(2026, 9, 20, 23, 0, 0, 0, time.UTC)))
	if !changed {
		t.Fatal("still stuck behind the old mark")
	}
	if s.LastSeq >= 9000 {
		t.Errorf("lastSeq = %d; a provisional number should not survive", s.LastSeq)
	}
}

func TestTheListOfSeenEventsIsBounded(t *testing.T) {
	at := time.Date(2026, 9, 20, 1, 0, 3, 0, time.UTC)
	s := State{}
	for i := int64(1); i <= maxSeenEvents+50; i++ {
		s, _, _ = Reduce(s, playEvent(i, i, "shot-on-goal", map[string]int{"CHI": 0, "FLA": 0}, at))
	}
	if len(s.SeenEvents) > maxSeenEvents {
		t.Errorf("kept %d event ids, want at most %d", len(s.SeenEvents), maxSeenEvents)
	}
	// The most recent are the ones kept: duplicates arrive soon, not hours later.
	if !folded(s, maxSeenEvents+50) || folded(s, 1) {
		t.Error("the list should keep the most recent events and let the oldest go")
	}
}
