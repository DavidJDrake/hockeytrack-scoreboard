// Package season is the NHL schedule as this project is willing to repeat it:
// fetched from HockeyTrack server-side, checked row by row, and cached.
//
// Two things use it. The picker shows it to an owner (GET /api/schedule), and
// a panel's schedule is checked against it when saved. It is public data; what
// is private is which games an owner picked, and that never goes near here.
//
// The file is another project's output and is headed for a web page and for a
// decision about what a panel shows, so nothing is passed through: a row is
// rebuilt from fields that passed a check, or it is left out.
package season

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
)

// MaxGames is more than two seasons of games. A file with more rows than
// this is not a schedule, and is refused whole.
const MaxGames = 3000

const (
	maxVenueRunes = 60
	maxNameRunes  = 40
	// maxTeams is several leagues' worth. More than this is not a list of
	// clubs.
	maxTeams = 128
)

// Game is one row. The JSON names are the API's.
type Game struct {
	GameID int64 `json:"gameId"`
	// Date is the NHL's game date, YYYY-MM-DD: what a schedule groups by. It
	// is not derived from Start -- a 10 PM Pacific game is the next day in
	// UTC and the same day to the league.
	Date  string `json:"date"`
	Start string `json:"start"` // RFC 3339, UTC, always readable
	Away  string `json:"away"`
	Home  string `json:"home"`
	Venue string `json:"venue,omitempty"`
	// Type is the NHL's game type: 1 preseason, 2 regular season, 3 playoffs.
	Type int `json:"type"`
}

// Season is every game that passed, by start then id.
type Season struct {
	Games []Game
	// Teams maps an abbreviation to the club's name, for the clubs that
	// passed. A game may name a club that is not here; it still lists.
	Teams map[string]string
	byID  map[int64]int
}

// Find returns the game with this id.
func (s Season) Find(id int64) (Game, bool) {
	i, ok := s.byID[id]
	if !ok {
		return Game{}, false
	}
	return s.Games[i], true
}

// Starts maps the given ids to their starts, leaving out any not in the
// season. It is the shape internal/schedule takes.
func (s Season) Starts(ids []int64) map[int64]string {
	out := make(map[int64]string, len(ids))
	for _, id := range ids {
		if g, ok := s.Find(id); ok {
			out[id] = g.Start
		}
	}
	return out
}

var ErrTooLarge = errors.New("season: more games than a schedule holds")

// Parse reads HockeyTrack's schedule.json. A row that fails a check is
// dropped and counted; the second value is how many were.
func Parse(body []byte) (Season, int, error) {
	var in struct {
		Teams map[string]string `json:"teams"`
		Games []struct {
			ID    int64  `json:"id"`
			Date  string `json:"date"`
			Start string `json:"start"`
			Away  string `json:"away"`
			Home  string `json:"home"`
			Type  int    `json:"type"`
			Venue string `json:"venue"`
		} `json:"games"`
	}
	if err := json.Unmarshal(body, &in); err != nil {
		return Season{}, 0, err
	}
	if len(in.Games) > MaxGames {
		return Season{}, 0, ErrTooLarge
	}
	if len(in.Teams) > maxTeams {
		return Season{}, 0, ErrTooLarge
	}
	s := Season{Games: make([]Game, 0, len(in.Games)), Teams: map[string]string{}, byID: map[int64]int{}}
	dropped := 0
	for ab, name := range in.Teams {
		if name = cleanLine(name, maxNameRunes); abbrev(ab) && name != "" {
			s.Teams[ab] = name
		} else {
			dropped++
		}
	}
	seen := map[int64]bool{}
	for _, g := range in.Games {
		start, err := time.Parse(time.RFC3339, g.Start)
		if err != nil || g.ID <= 0 || seen[g.ID] || !abbrev(g.Away) || !abbrev(g.Home) || g.Away == g.Home ||
			g.Type < 1 || g.Type > 4 || !gameDate(g.Date) {
			dropped++
			continue
		}
		seen[g.ID] = true
		s.Games = append(s.Games, Game{GameID: g.ID, Date: g.Date, Start: start.UTC().Format(time.RFC3339),
			Away: g.Away, Home: g.Home, Venue: cleanVenue(g.Venue), Type: g.Type})
	}
	sort.Slice(s.Games, func(i, j int) bool {
		a, b := s.Games[i], s.Games[j]
		if a.Start != b.Start {
			return a.Start < b.Start
		}
		return a.GameID < b.GameID
	})
	for i, g := range s.Games {
		s.byID[g.GameID] = i
	}
	return s, dropped, nil
}

func abbrev(s string) bool {
	if len(s) < 2 || len(s) > 4 {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 'A' || s[i] > 'Z' {
			return false
		}
	}
	return true
}

// gameDate: exactly YYYY-MM-DD, and a day that exists.
func gameDate(s string) bool {
	if len(s) != 10 {
		return false
	}
	t, err := time.Parse("2006-01-02", s)
	return err == nil && t.Format("2006-01-02") == s
}

// cleanVenue makes a building's name one short printable line.
func cleanVenue(v string) string { return cleanLine(v, maxVenueRunes) }

func cleanLine(v string, maxRunes int) string {
	v = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || r == unicode.ReplacementChar {
			return ' '
		}
		return r
	}, v)
	v = strings.Join(strings.Fields(v), " ")
	if r := []rune(v); len(r) > maxRunes {
		v = strings.TrimSpace(string(r[:maxRunes]))
	}
	return v
}

// Cache holds the last season read, for the life of the process.
//
// Fresh for FreshFor; after that the next caller fetches again. If that
// fetch fails, or what it returns does not parse or is empty, the last good
// season is served for up to StaleFor: HockeyTrack being down for an hour
// must not stop an owner from opening the picker, and a broken file must not
// replace a good one. Past StaleFor there is nothing to stand behind, and
// the error is returned.
type Cache struct {
	Fetch    func(ctx context.Context) ([]byte, error)
	Now      func() time.Time
	FreshFor time.Duration
	StaleFor time.Duration
	// Dropped is told how many rows a parse left out, when any were.
	Dropped func(n int)

	mu     sync.Mutex
	season Season
	at     time.Time
}

var ErrEmpty = errors.New("season: the schedule has no games in it")

func (c *Cache) Get(ctx context.Context) (Season, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.Now()
	have := !c.at.IsZero()
	if have && now.Sub(c.at) < c.FreshFor {
		return c.season, nil
	}
	s, err := c.read(ctx)
	if err == nil {
		c.season, c.at = s, now
		return s, nil
	}
	if have && now.Sub(c.at) < c.StaleFor {
		return c.season, nil
	}
	return Season{}, err
}

func (c *Cache) read(ctx context.Context) (Season, error) {
	body, err := c.Fetch(ctx)
	if err != nil {
		return Season{}, err
	}
	s, dropped, err := Parse(body)
	if err != nil {
		return Season{}, err
	}
	if dropped > 0 && c.Dropped != nil {
		c.Dropped(dropped)
	}
	if len(s.Games) == 0 {
		return Season{}, ErrEmpty
	}
	return s, nil
}
