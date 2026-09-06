package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/reduce"
)

func ebEvent(t *testing.T, detailType, file string) events.CloudWatchEvent {
	t.Helper()
	b, err := os.ReadFile("../../internal/reduce/testdata/events/" + file)
	if err != nil {
		t.Fatal(err)
	}
	return events.CloudWatchEvent{Source: "hockeytrack.poller", DetailType: detailType, Detail: json.RawMessage(b), Time: time.Date(2025, 10, 7, 22, 31, 6, 0, time.UTC)}
}

func TestHandlerPublishesRetainedStateOnChange(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games"}
	ctx := context.Background()

	if err := h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json")); err != nil {
		t.Fatal(err)
	}
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.roster", "roster.json")); err != nil {
		t.Fatal(err) // roster changes nothing visible: no publish
	}
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json")); err != nil {
		t.Fatal(err)
	}
	if len(pub.Messages) != 2 {
		t.Fatalf("published %d messages, want 2 (status, clock)", len(pub.Messages))
	}
	m := pub.Messages[1]
	if m.Topic != "hockeytrack/games/2025020001/state" || !m.Retain {
		t.Errorf("message = %+v", m)
	}
	if !strings.Contains(string(m.Payload), `"sog":17`) || strings.Contains(string(m.Payload), "lastSeq") {
		t.Errorf("payload = %s", m.Payload)
	}
	s, found, _ := st.Get(ctx, 2025020001)
	if !found || s.Version != 3 || len(s.Roster) != 4 {
		t.Errorf("stored = found:%v version:%d roster:%d", found, s.Version, len(s.Roster))
	}
}

func TestHandlerRetriesOnceOnConflict(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games"}
	ctx := context.Background()
	_ = h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json"))
	// Simulate a concurrent writer bumping the version between Get and Put.
	conflicting := &conflictOnce{Store: st}
	h.Store = conflicting
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json")); err != nil {
		t.Fatalf("expected the retry to succeed: %v", err)
	}
	if conflicting.puts != 2 {
		t.Errorf("puts = %d, want 2 (conflict then success)", conflicting.puts)
	}
}

// conflictOnce wraps a Store and fails the first Put with ErrConflict.
type conflictOnce struct {
	gamestore.Store
	puts int
}

func (c *conflictOnce) Put(ctx context.Context, s reduce.State) error {
	c.puts++
	if c.puts == 1 {
		return gamestore.ErrConflict
	}
	return c.Store.Put(ctx, s)
}
