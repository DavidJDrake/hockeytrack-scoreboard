package reduce

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"
)

// Event is one EventBridge event from the hockeytrack bus.
type Event struct {
	DetailType string
	Detail     json.RawMessage
	Time       time.Time
}

// Minimal decoders for HockeyTrack's detail-types. Only the fields the
// scoreboard needs; unknown fields are ignored.
type statusDetail struct {
	GameID    int64          `json:"gameId"`
	GameState string         `json:"gameState"`
	Score     map[string]int `json:"score"`
}

type clockDetail struct {
	GameID           int64          `json:"gameId"`
	GameState        string         `json:"gameState"`
	Period           int            `json:"period"`
	PeriodType       string         `json:"periodType"`
	SecondsRemaining int            `json:"secondsRemaining"`
	Running          bool           `json:"running"`
	InIntermission   bool           `json:"inIntermission"`
	SituationCode    string         `json:"situationCode"`
	HomeTeam         string         `json:"homeTeam"`
	AwayTeam         string         `json:"awayTeam"`
	Score            map[string]int `json:"score"`
	Shots            map[string]int `json:"shots"`
	ObservedAt       time.Time      `json:"observedAt"`
}

type playDetail struct {
	GameID       int64          `json:"gameId"`
	Seq          int64          `json:"seq"`
	PlayType     string         `json:"playType"`
	HomeTeam     string         `json:"homeTeam"`
	AwayTeam     string         `json:"awayTeam"`
	ActingTeam   string         `json:"actingTeam"`
	ScoringTeam  string         `json:"scoringTeam"`
	Period       int            `json:"period"`
	TimeInPeriod string         `json:"timeInPeriod"`
	Score        map[string]int `json:"score"`
	Raw          struct {
		PeriodDescriptor struct {
			Number     int    `json:"number"`
			PeriodType string `json:"periodType"`
		} `json:"periodDescriptor"`
		Details struct {
			TypeCode            string `json:"typeCode"`
			Duration            int    `json:"duration"`
			CommittedByPlayerID int64  `json:"committedByPlayerId"`
			ServedByPlayerID    int64  `json:"servedByPlayerId"`
			ScoringPlayerID     int64  `json:"scoringPlayerId"`
		} `json:"details"`
	} `json:"raw"`
}

type rosterDetail struct {
	GameID   int64  `json:"gameId"`
	HomeTeam string `json:"homeTeam"`
	AwayTeam string `json:"awayTeam"`
	Players  []struct {
		PlayerID int64  `json:"playerId"`
		Team     string `json:"team"`
		Number   int    `json:"number"`
	} `json:"players"`
}

type finalDetail struct {
	GameID   int64          `json:"gameId"`
	HomeTeam string         `json:"homeTeam"`
	AwayTeam string         `json:"awayTeam"`
	Score    map[string]int `json:"score"`
}

// Reduce folds one event into the state. The bool reports whether the
// public document changed and should be republished.
func Reduce(s State, e Event) (State, bool, error) {
	s.V = 1
	if s.Roster == nil {
		s.Roster = map[int64]int{}
	}
	if s.Penalties == nil {
		s.Penalties = []Penalty{}
	}
	stamp := func() { s.AsOf = e.Time.UTC().UnixMilli() }

	switch e.DetailType {
	case "nhl.game.status":
		var d statusDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("status: %w", err)
		}
		s.GameID = d.GameID
		s.GameState = mapGameState(d.GameState)
		// The status event carries no home/away; the score map's key order
		// is not stable, so only fill abbreviations if we have none yet and
		// let the clock/play events (which do carry them) correct it.
		s.applyScore(d.Score)
		if s.GameState == "PRE" {
			s.Penalties = []Penalty{}
		}
		stamp()
		return s, true, nil

	case "nhl.game.clock":
		var d clockDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("clock: %w", err)
		}
		if obs := d.ObservedAt.UnixMilli(); obs < s.ObservedAt {
			return s, false, nil // out-of-order heartbeat
		} else {
			s.ObservedAt = obs
		}
		s.GameID = d.GameID
		s.GameState = mapGameState(d.GameState)
		s.setTeams(d.AwayTeam, d.HomeTeam)
		s.applyScore(d.Score)
		s.Away.SOG, s.Home.SOG = d.Shots[d.AwayTeam], d.Shots[d.HomeTeam]
		s.OTLen = OTLength(d.GameID)
		if d.Period > 0 {
			s.Period = Period{Number: d.Period, Type: d.PeriodType, Label: PeriodLabel(d.Period, d.PeriodType)}
		}
		s.Clock = Clock{Seconds: d.SecondsRemaining, Running: d.Running, Intermission: d.InIntermission}
		s.Situation = ParseSituation(d.SituationCode, d.AwayTeam, d.HomeTeam)
		s.tickPenalties()
		stamp()
		return s, true, nil

	case "nhl.game.play":
		return s.applyPlay(e)

	case "nhl.game.roster":
		var d rosterDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("roster: %w", err)
		}
		s.GameID = d.GameID
		s.setTeams(d.AwayTeam, d.HomeTeam)
		for _, p := range d.Players {
			s.Roster[p.PlayerID] = p.Number
		}
		return s, false, nil // roster alone changes nothing visible

	case "nhl.game.final":
		var d finalDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("final: %w", err)
		}
		s.GameID = d.GameID
		s.setTeams(d.AwayTeam, d.HomeTeam)
		s.applyScore(d.Score)
		s.GameState = "FINAL"
		s.Clock.Running = false
		s.Penalties = []Penalty{}
		s.Situation = Situation{Code: s.Situation.Code}
		stamp()
		return s, true, nil
	}
	return s, false, nil
}

// mapGameState collapses the NHL's states into the three the panel shows.
func mapGameState(nhl string) string {
	switch nhl {
	case "LIVE", "CRIT":
		return "LIVE"
	case "FINAL", "OFF":
		return "FINAL"
	default:
		return "PRE"
	}
}

func (s *State) setTeams(away, home string) {
	if away != "" {
		s.Away.Abbrev = away
		s.Away.Color = TeamColor(away)
	}
	if home != "" {
		s.Home.Abbrev = home
		s.Home.Color = TeamColor(home)
	}
}

func (s *State) applyScore(score map[string]int) {
	if s.Away.Abbrev == "" && s.Home.Abbrev == "" && len(score) == 2 {
		// First sight of the game via a status event: we know the two
		// abbreviations but not which is home. Use alphabetical order for
		// now; the first clock or play event (which carry home/away) fixes it.
		keys := make([]string, 0, 2)
		for k := range score {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		s.setTeams(keys[0], keys[1])
	}
	if v, ok := score[s.Away.Abbrev]; ok {
		s.Away.Score = v
	}
	if v, ok := score[s.Home.Abbrev]; ok {
		s.Home.Score = v
	}
}

// applyPlay, tickPenalties and TeamColor are defined in Tasks 4-6; these are
// temporary stubs so the package compiles until then.
func (s State) applyPlay(e Event) (State, bool, error) { return s, false, nil }
func (s *State) tickPenalties()                        {}
func TeamColor(abbrev string) string                   { return "888888" }
