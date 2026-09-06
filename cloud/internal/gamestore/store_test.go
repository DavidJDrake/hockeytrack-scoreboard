package gamestore

import (
	"context"
	"errors"
	"testing"

	"github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"

	"hockeytrack-scoreboard/internal/reduce"
)

func TestFakeStoreOptimisticLock(t *testing.T) {
	st := NewFake()
	ctx := context.Background()
	if _, found, _ := st.Get(ctx, 1); found {
		t.Fatal("empty store reported a game")
	}
	s := reduce.State{V: 1, GameID: 1, GameState: "LIVE"}
	if err := st.Put(ctx, s); err != nil {
		t.Fatal(err)
	}
	got, found, _ := st.Get(ctx, 1)
	if !found || got.Version != 1 {
		t.Fatalf("after first put: found=%v version=%d", found, got.Version)
	}
	stale := s // Version 0
	if err := st.Put(ctx, stale); !errors.Is(err, ErrConflict) {
		t.Errorf("stale put err = %v, want ErrConflict", err)
	}
	got.GameState = "FINAL"
	if err := st.Put(ctx, got); err != nil {
		t.Fatal(err)
	}
	got2, _, _ := st.Get(ctx, 1)
	if got2.Version != 2 || got2.GameState != "FINAL" {
		t.Errorf("after second put: %+v", got2)
	}
	active, _ := st.ListActive(ctx)
	if len(active) != 1 {
		t.Errorf("ListActive = %d items", len(active))
	}
}

// TestStateDynamoRoundTrip verifies that reduce.State's internal bookkeeping
// fields survive a MarshalMap/UnmarshalMap round trip through the
// attributevalue package (the same path dynamo.go uses). If any of these
// silently vanish, the reducer forgets its own history between Lambda
// invocations.
func TestStateDynamoRoundTrip(t *testing.T) {
	want := reduce.State{
		V:         1,
		GameID:    2025020123,
		GameState: "LIVE",
		AsOf:      1700000000000,
		Away:      reduce.Team{Abbrev: "TBL", Score: 2, SOG: 10, Color: "#00285e"},
		Home:      reduce.Team{Abbrev: "NYR", Score: 1, SOG: 12, Color: "#0038a8"},
		Period:    reduce.Period{Number: 2, Type: "REG", Label: "2"},
		Clock:     reduce.Clock{Seconds: 754, Running: true},
		Situation: reduce.Situation{Code: "1551"},
		Penalties: []reduce.Penalty{{Team: "TBL", Number: 86, Seconds: 45, Type: "MIN", StartT: 1200, Duration: 120}},
		LastGoal:  &reduce.Goal{Team: "TBL", Number: 21, AsOf: 1699999000000},

		LastSeq:    42,
		Roster:     map[int64]int{8478010: 86, 8471675: 91},
		OTLen:      300,
		ObservedAt: 1700000005000,
		Version:    7,
	}

	av, err := attributevalue.MarshalMap(item{State: want})
	if err != nil {
		t.Fatalf("MarshalMap: %v", err)
	}

	var got item
	if err := attributevalue.UnmarshalMap(av, &got); err != nil {
		t.Fatalf("UnmarshalMap: %v", err)
	}

	if got.LastSeq != want.LastSeq {
		t.Errorf("LastSeq = %d, want %d", got.LastSeq, want.LastSeq)
	}
	if len(got.Roster) != len(want.Roster) {
		t.Errorf("Roster = %v, want %v", got.Roster, want.Roster)
	}
	for k, v := range want.Roster {
		if got.Roster[k] != v {
			t.Errorf("Roster[%d] = %d, want %d", k, got.Roster[k], v)
		}
	}
	if got.OTLen != want.OTLen {
		t.Errorf("OTLen = %d, want %d", got.OTLen, want.OTLen)
	}
	if got.ObservedAt != want.ObservedAt {
		t.Errorf("ObservedAt = %d, want %d", got.ObservedAt, want.ObservedAt)
	}
	if got.Version != want.Version {
		t.Errorf("Version = %d, want %d", got.Version, want.Version)
	}
	if got.GameID != want.GameID || got.GameState != want.GameState {
		t.Errorf("public fields did not round-trip: %+v", got.State)
	}
}
