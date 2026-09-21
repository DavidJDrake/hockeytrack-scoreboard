package main

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/reduce"
	"hockeytrack-scoreboard/internal/summary"
)

func setup(t *testing.T) (*publisher, *gamestore.Fake, *iotpub.Fake, *time.Time) {
	t.Helper()
	store, pub := gamestore.NewFake(), &iotpub.Fake{}
	clock := time.Date(2026, 9, 21, 23, 30, 0, 0, time.UTC)
	return &publisher{store: store, pub: pub, now: func() time.Time { return clock }}, store, pub, &clock
}

func put(t *testing.T, store *gamestore.Fake, awayScore int) {
	t.Helper()
	got, _, _ := store.Get(context.Background(), 2026010011)
	s := reduce.State{GameID: 2026010011, GameState: "LIVE", Version: got.Version,
		Away: reduce.Team{Abbrev: "TBL", Score: awayScore}, Home: reduce.Team{Abbrev: "NYR"}}
	if err := store.Put(context.Background(), s); err != nil {
		t.Fatal(err)
	}
}

func TestItPublishesOneRetainedDocumentToOneTopic(t *testing.T) {
	p, store, pub, _ := setup(t)
	put(t, store, 1)
	if err := p.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(pub.Messages) != 1 {
		t.Fatalf("published %d messages", len(pub.Messages))
	}
	m := pub.Messages[0]
	if m.Topic != "hockeytrack/games/summary" || !m.Retain {
		t.Errorf("got topic %q retain %v", m.Topic, m.Retain)
	}
	var doc summary.Doc
	if err := json.Unmarshal(m.Payload, &doc); err != nil || len(doc.Games) != 1 || doc.Games[0].AwayScore != 1 {
		t.Errorf("payload %s (%v)", m.Payload, err)
	}
}

func TestAnUnchangedListIsNotSentAgainUntilItIsDue(t *testing.T) {
	p, store, pub, clock := setup(t)
	put(t, store, 1)
	for i := 0; i < 9; i++ {
		if err := p.run(context.Background()); err != nil {
			t.Fatal(err)
		}
		*clock = clock.Add(time.Minute)
	}
	if len(pub.Messages) != 1 {
		t.Fatalf("nine unchanged minutes published %d times", len(pub.Messages))
	}
	*clock = clock.Add(time.Minute) // ten minutes since the first
	if err := p.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(pub.Messages) != 2 {
		t.Errorf("the retained copy was not refreshed when due: %d", len(pub.Messages))
	}
}

func TestAGoalIsSentTheMinuteItIsSeen(t *testing.T) {
	p, store, pub, clock := setup(t)
	put(t, store, 1)
	_ = p.run(context.Background())
	*clock = clock.Add(time.Minute)
	put(t, store, 2)
	if err := p.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(pub.Messages) != 2 {
		t.Fatalf("published %d times", len(pub.Messages))
	}
}

func TestAFailedPublishIsTriedAgainTheNextMinute(t *testing.T) {
	p, store, pub, clock := setup(t)
	put(t, store, 1)
	pub.FailTopics = map[string]bool{topic: true}
	if err := p.run(context.Background()); err == nil {
		t.Fatal("a failed publish must fail the run, so the alarm on errors sees it")
	}
	pub.FailTopics = nil
	*clock = clock.Add(time.Minute)
	if err := p.run(context.Background()); err != nil || len(pub.Messages) != 1 {
		t.Errorf("not retried: %v, %d", err, len(pub.Messages))
	}
}

func TestAnEmptyTableStillPublishesSoOldScoresComeDown(t *testing.T) {
	p, _, pub, _ := setup(t)
	if err := p.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(pub.Messages) != 1 || string(pub.Messages[0].Payload) == "" {
		t.Fatalf("got %+v", pub.Messages)
	}
	var doc summary.Doc
	_ = json.Unmarshal(pub.Messages[0].Payload, &doc)
	if doc.Games == nil || len(doc.Games) != 0 {
		t.Errorf("payload %s", pub.Messages[0].Payload)
	}
}
