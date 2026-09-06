package today

import (
	"os"
	"testing"
	"time"
)

func TestBuildPicksTodayAndCarriesLateGames(t *testing.T) {
	sched, err := os.ReadFile("testdata/schedule.json")
	if err != nil {
		t.Fatal(err)
	}
	// 2026-09-20 09:00 ET: the day after the preseason opener.
	now := time.Date(2026, 9, 20, 13, 0, 0, 0, time.UTC)
	doc, err := Build(sched, map[int64]string{2026010001: "FINAL", 2026010007: "LIVE"}, now)
	if err != nil {
		t.Fatal(err)
	}
	var today, yesterday int
	for _, g := range doc.Games {
		switch {
		case g.Start >= "2026-09-20T04:00:00Z" && g.Start < "2026-09-21T04:00:00Z":
			today++
		default:
			yesterday++
			if g.State == "FINAL" {
				t.Errorf("finished game from yesterday should not be listed: %+v", g)
			}
		}
	}
	if today == 0 {
		t.Error("no games for 2026-09-20 found")
	}
	if yesterday != 1 {
		t.Errorf("late/unfinished games from yesterday = %d, want 1 (game 2026010007 LIVE)", yesterday)
	}
	for i := 1; i < len(doc.Games); i++ {
		if doc.Games[i].Start < doc.Games[i-1].Start {
			t.Fatal("games not sorted by start")
		}
	}
	// After noon ET, yesterday's games drop even if not FINAL in our table.
	doc, _ = Build(sched, map[int64]string{2026010007: "LIVE"}, time.Date(2026, 9, 20, 17, 0, 0, 0, time.UTC))
	for _, g := range doc.Games {
		if g.GameID == 2026010007 {
			t.Error("yesterday's game still listed after noon ET")
		}
	}
	if doc.GeneratedAt == 0 {
		t.Error("generatedAt unset")
	}
}
