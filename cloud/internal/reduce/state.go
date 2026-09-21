package reduce

import (
	"encoding/json"
	"fmt"
)

type Team struct {
	Abbrev string `json:"abbrev" dynamodbav:"abbrev"`
	Score  int    `json:"score" dynamodbav:"score"`
	SOG    int    `json:"sog" dynamodbav:"sog"`
	Color  string `json:"color" dynamodbav:"color"`
}

type Period struct {
	Number int    `json:"number" dynamodbav:"number"`
	Type   string `json:"type" dynamodbav:"type"`
	Label  string `json:"label" dynamodbav:"label"`
}

type Clock struct {
	Seconds      int  `json:"seconds" dynamodbav:"seconds"`
	Running      bool `json:"running" dynamodbav:"running"`
	Intermission bool `json:"intermission" dynamodbav:"intermission"`
}

type Situation struct {
	Code     string `json:"code" dynamodbav:"code"`
	PP       string `json:"pp,omitempty" dynamodbav:"pp,omitempty"`
	EmptyNet string `json:"emptyNet,omitempty" dynamodbav:"emptyNet,omitempty"`
}

type Penalty struct {
	Team       string `json:"team" dynamodbav:"team"`
	Number     int    `json:"number" dynamodbav:"number"`
	Seconds    int    `json:"seconds" dynamodbav:"seconds"`
	Type       string `json:"type" dynamodbav:"type"` // MIN, MAJ, BEN, MIS, MAT
	EndsOnGoal bool   `json:"endsOnGoal" dynamodbav:"endsOnGoal"`
	StartT     int    `json:"-" dynamodbav:"startT"`   // absolute game seconds when assessed
	Duration   int    `json:"-" dynamodbav:"duration"` // seconds
}

type Goal struct {
	Team   string `json:"team" dynamodbav:"team"`
	Number int    `json:"number" dynamodbav:"number"`
	AsOf   int64  `json:"asOf" dynamodbav:"asOf"`
	// Period and Time say when in the game it was scored ("2", "OT";
	// "12:34" elapsed), for a panel that shows the last goal after the flash
	// has gone. AsOf cannot: it is when the event arrived. Either is left
	// out when the play did not carry something that reads as one -- they
	// are drawn on a panel, so they are checked here, not there.
	Period   string `json:"period,omitempty" dynamodbav:"period,omitempty"`
	Time     string `json:"time,omitempty" dynamodbav:"time,omitempty"`
	PlayerID int64  `json:"-" dynamodbav:"playerId"` // bookkeeping: lets the roster fold backfill Number if it arrives late
}

// State is the document devices render. Fields tagged json:"-" are
// bookkeeping the reducer needs between events; they persist in DynamoDB
// but never reach the topic.
type State struct {
	V         int    `json:"v" dynamodbav:"v"`
	GameID    int64  `json:"gameId" dynamodbav:"gameId"`
	GameState string `json:"state" dynamodbav:"state"` // PRE | LIVE | FINAL
	AsOf      int64  `json:"asOf" dynamodbav:"asOf"`
	// SeenAt is when a clock heartbeat last confirmed this document. AsOf
	// cannot do that job: it is the clock's anchor, and it deliberately
	// stands still while the feed repeats itself. Panels judge freshness by
	// whether the document changed, so something in it has to.
	SeenAt int64 `json:"seenAt,omitempty" dynamodbav:"seenAt"`
	// FinalAt is when the game ended: the moment it was first seen FINAL,
	// set once and never moved. A final comes down one hold after THIS, not
	// after whenever a panel happened to first see it -- a panel given a game
	// that ended two hours ago, with a one-hour hold, should show nothing.
	FinalAt   int64     `json:"finalAt,omitempty" dynamodbav:"finalAt"`
	Away      Team      `json:"away" dynamodbav:"away"`
	Home      Team      `json:"home" dynamodbav:"home"`
	Period    Period    `json:"period" dynamodbav:"period"`
	Clock     Clock     `json:"clock" dynamodbav:"clock"`
	Situation Situation `json:"situation" dynamodbav:"situation"`
	Penalties []Penalty `json:"penalties" dynamodbav:"penalties"`
	LastGoal  *Goal     `json:"lastGoal,omitempty" dynamodbav:"lastGoal,omitempty"`
	Start     string    `json:"start,omitempty" dynamodbav:"start,omitempty"`

	// LastSeq is the highest real sort order folded. It decides nothing when
	// plays carry an eventId; see applyPlay.
	LastSeq int64 `json:"-" dynamodbav:"lastSeq"`
	// SeenEvents is the plays already folded, by the NHL's eventId, newest
	// last, bounded. It is what makes a play delivered twice fold once.
	SeenEvents []int64       `json:"-" dynamodbav:"seenEvents,omitempty"`
	Roster     map[int64]int `json:"-" dynamodbav:"roster"`
	OTLen      int           `json:"-" dynamodbav:"otLen"`
	ObservedAt int64         `json:"-" dynamodbav:"observedAt"`
	Version    int64         `json:"-" dynamodbav:"version"` // optimistic lock
}

// JSON marshals the public document; nil slices become [] so devices can
// index them without null checks.
func (s State) JSON() ([]byte, error) {
	if s.Penalties == nil {
		s.Penalties = []Penalty{}
	}
	return json.Marshal(s)
}

// ParseSituation decodes the NHL's four-digit situation code
// (away goalie, away skaters, home skaters, home goalie).
func ParseSituation(code, away, home string) Situation {
	s := Situation{Code: code}
	if len(code) != 4 {
		return s
	}
	var d [4]int
	for i := 0; i < 4; i++ {
		if code[i] < '0' || code[i] > '9' {
			return s
		}
		d[i] = int(code[i] - '0')
	}
	awayGoalie, awaySk, homeSk, homeGoalie := d[0], d[1], d[2], d[3]

	// A pulled goalie adds an extra attacker, inflating that team's skater
	// count without it being a power play; back that skater out before
	// comparing counts so an empty-net situation alone doesn't register as
	// a PP.
	effAway, effHome := awaySk, homeSk
	if awayGoalie == 0 && awaySk > 0 {
		effAway--
	}
	if homeGoalie == 0 && homeSk > 0 {
		effHome--
	}
	switch {
	case effHome > effAway:
		s.PP = home
	case effAway > effHome:
		s.PP = away
	}
	switch {
	case awayGoalie == 0 && awaySk > 0:
		s.EmptyNet = away
	case homeGoalie == 0 && homeSk > 0:
		s.EmptyNet = home
	}
	return s
}

// PeriodLabel renders 1/2/3, OT, 2OT…, SO.
func PeriodLabel(number int, periodType string) string {
	switch {
	case number == 0:
		return ""
	case periodType == "SO":
		return "SO"
	case periodType == "OT" && number <= 4:
		return "OT"
	case periodType == "OT":
		return fmt.Sprintf("%dOT", number-3)
	default:
		return fmt.Sprintf("%d", number)
	}
}

// PeriodTime is a time in a period as the feed writes it, "MM:SS", or "" if
// “s“ is anything else. The string goes on to a panel that draws it, so
// what passes is spelled out: digits, one colon, seconds under sixty,
// minutes that a period (or a playoff overtime) could hold.
func PeriodTime(s string) string {
	if len(s) != 5 || s[2] != ':' {
		return ""
	}
	for _, i := range []int{0, 1, 3, 4} {
		if s[i] < '0' || s[i] > '9' {
			return ""
		}
	}
	if s[3] > '5' || s[0] > '2' || (s[0] == '2' && (s[1] != '0' || s[3:] != "00")) {
		return ""
	}
	return s
}

// OTLength is the overtime period length implied by the game id: the two
// digits after the season are the game type (01 preseason, 02 regular
// season, 03 playoffs); only playoff overtime is a full 20 minutes.
func OTLength(gameID int64) int {
	if (gameID/10000)%100 == 3 {
		return 1200
	}
	return 300
}

// PeriodLength is 20 minutes for regulation and otLen after that.
func PeriodLength(period, otLen int) int {
	if period <= 3 {
		return 1200
	}
	if otLen == 0 {
		return 300
	}
	return otLen
}

// GameTime converts (period, seconds elapsed in period) to absolute game
// seconds.
func GameTime(period, elapsedInPeriod, otLen int) int {
	t := 0
	for p := 1; p < period; p++ {
		t += PeriodLength(p, otLen)
	}
	return t + elapsedInPeriod
}
