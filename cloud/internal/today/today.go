// Package today builds the document the scoreboard's owner picks a game
// from: today's games in Eastern time, plus yesterday's unfinished games
// until noon ET.
package today

import (
	"encoding/json"
	"sort"
	"time"
)

type Game struct {
	GameID int64  `json:"gameId"`
	Away   string `json:"away"`
	Home   string `json:"home"`
	Start  string `json:"start"`
	State  string `json:"state"`
}

type Doc struct {
	GeneratedAt int64  `json:"generatedAt"`
	Games       []Game `json:"games"`
}

type schedule struct {
	Games []struct {
		ID    int64  `json:"id"`
		Date  string `json:"date"`
		Start string `json:"start"`
		Away  string `json:"away"`
		Home  string `json:"home"`
	} `json:"games"`
}

var eastern = mustLoad("America/New_York")

func mustLoad(name string) *time.Location {
	l, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return l
}

// Build lists today's games (ET) plus yesterday's unfinished ones until
// noon ET, with each game's current reducer state (PRE when unknown).
func Build(scheduleJSON []byte, states map[int64]string, now time.Time) (Doc, error) {
	var s schedule
	if err := json.Unmarshal(scheduleJSON, &s); err != nil {
		return Doc{}, err
	}
	et := now.In(eastern)
	today := et.Format("2006-01-02")
	yesterday := et.AddDate(0, 0, -1).Format("2006-01-02")
	carryYesterday := et.Hour() < 12
	doc := Doc{GeneratedAt: now.UnixMilli(), Games: []Game{}}
	for _, g := range s.Games {
		state, tracked := states[g.ID]
		switch {
		case g.Date == today:
			// A missing entry means the reducer hasn't seen this game yet
			// (it hasn't started), so it defaults to PRE.
		case g.Date == yesterday && carryYesterday && tracked && state != "FINAL":
			// Only a game the reducer is still actively tracking carries
			// over; one absent from the store has already gone FINAL and
			// aged out of gamestore.ListActive.
		default:
			continue
		}
		if state == "" {
			state = "PRE"
		}
		doc.Games = append(doc.Games, Game{GameID: g.ID, Away: g.Away, Home: g.Home, Start: g.Start, State: state})
	}
	sort.Slice(doc.Games, func(i, j int) bool { return doc.Games[i].Start < doc.Games[j].Start })
	return doc, nil
}
