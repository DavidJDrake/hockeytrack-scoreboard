package main

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/reduce"
	"hockeytrack-scoreboard/internal/today"
)

// A two-night schedule in the shape HockeyTrack publishes: one game last
// night at 22:30 ET (02:30Z), one tonight.
const lateSchedule = `{"games":[
	{"id":2026020101,"date":"2026-10-01","start":"2026-10-02T02:30:00Z","away":"LAK","home":"SJS"},
	{"id":2026020102,"date":"2026-10-02","start":"2026-10-02T23:00:00Z","away":"TBL","home":"NYR"}
]}`

// 2026-10-02 00:45 ET, forty-five minutes after the calendar rolled over
// while last night's late game is still in its third period.
var pastMidnight = time.Date(2026, 10, 2, 4, 45, 0, 0, time.UTC)

func listGames(t *testing.T, store gamestore.Store) today.Doc {
	t.Helper()
	body, err := gamesDoc(context.Background(), []byte(lateSchedule), store, pastMidnight)
	if err != nil {
		t.Fatal(err)
	}
	var doc today.Doc
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatal(err)
	}
	return doc
}

func ids(doc today.Doc) []int64 {
	var out []int64
	for _, g := range doc.Games {
		out = append(out, g.GameID)
	}
	return out
}

// The panel's own list keeps a game that is still playing after midnight; the
// site's list must agree with it, or the owner cannot pick the game they are
// watching (SCO-20).
func TestGamesListKeepsAGameStillLivePastMidnight(t *testing.T) {
	store := gamestore.NewFake()
	_ = store.Put(context.Background(), reduce.State{GameID: 2026020101, GameState: "LIVE", AsOf: pastMidnight.UnixMilli()})
	doc := listGames(t, store)
	if got := ids(doc); len(got) != 2 || got[0] != 2026020101 || got[1] != 2026020102 {
		t.Fatalf("games = %v, want last night's LIVE game then tonight's", got)
	}
	if doc.Games[0].State != "LIVE" || doc.Games[1].State != "PRE" {
		t.Errorf("states = %s, %s; want LIVE, PRE", doc.Games[0].State, doc.Games[1].State)
	}
}

// A game the reducer has marked FINAL is over, and one it has aged out of
// the table is over too: neither carries past midnight.
func TestGamesListDropsAFinishedGamePastMidnight(t *testing.T) {
	store := gamestore.NewFake()
	_ = store.Put(context.Background(), reduce.State{GameID: 2026020101, GameState: "FINAL", AsOf: pastMidnight.UnixMilli()})
	if got := ids(listGames(t, store)); len(got) != 1 || got[0] != 2026020102 {
		t.Errorf("games = %v, want tonight's game only", got)
	}
	if got := ids(listGames(t, gamestore.NewFake())); len(got) != 1 || got[0] != 2026020102 {
		t.Errorf("games = %v, want tonight's game only", got)
	}
}

type downStore struct{ gamestore.Store }

func (downStore) ListActive(context.Context) ([]reduce.State, error) {
	return nil, errors.New("throttled")
}

// The list is the page. A games table that is down must not empty it: the
// site says less (today's games, no carry-over), it does not say nothing.
func TestGamesListSurvivesTheGamesTableBeingDown(t *testing.T) {
	if got := ids(listGames(t, downStore{})); len(got) != 1 || got[0] != 2026020102 {
		t.Errorf("games = %v, want tonight's game only", got)
	}
}
