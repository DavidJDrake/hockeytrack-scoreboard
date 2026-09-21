package main

import (
	"errors"

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

// conflictN fails the first n Puts with ErrConflict.
type conflictN struct {
	gamestore.Store
	n, puts int
}

func (c *conflictN) Put(ctx context.Context, s reduce.State) error {
	c.puts++
	if c.puts <= c.n {
		return gamestore.ErrConflict
	}
	return c.Store.Put(ctx, s)
}

// Measured on 2026-09-20: 6 to 12 events an hour failed with a version
// conflict, all evening. A live game is a heartbeat every five seconds plus
// plays arriving in bursts, each its own invocation, all writing one row. One
// retry was not enough, and what happened next was the costly part: the event
// went back to Lambda, which tries again a minute later, then two minutes
// after that, then drops it. A penalty could reach a panel minutes late, or
// never, and nothing said so.
func TestSeveralConflictsInARowAreStillFoldedAtOnce(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	var slept []time.Duration
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games",
		Sleep: func(_ context.Context, d time.Duration) error { slept = append(slept, d); return nil }}
	ctx := context.Background()
	_ = h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json"))
	busy := &conflictN{Store: st, n: 4}
	h.Store = busy
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json")); err != nil {
		t.Fatalf("four conflicts in a row should still end in a write: %v", err)
	}
	if busy.puts != 5 {
		t.Errorf("puts = %d, want 5", busy.puts)
	}
	if len(slept) != 4 {
		t.Fatalf("paused %d times between 5 attempts, want 4", len(slept))
	}
	for i, d := range slept {
		if d <= 0 || d > maxConflictPause {
			t.Errorf("pause %d was %v, want between 0 and %v", i, d, maxConflictPause)
		}
	}
}

func TestEveryRetryStartsFromAFreshRead(t *testing.T) {
	// The point of retrying is that somebody else wrote first. Folding the
	// event into the state read BEFORE their write would undo it.
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games",
		Sleep: func(context.Context, time.Duration) error { return nil }}
	ctx := context.Background()
	_ = h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json"))
	reads := &countingGets{Store: &conflictN{Store: st, n: 3}}
	h.Store = reads
	_ = h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json"))
	if reads.gets != 4 {
		t.Errorf("gets = %d, want one fresh read per attempt (4)", reads.gets)
	}
}

type countingGets struct {
	gamestore.Store
	gets int
}

func (c *countingGets) Get(ctx context.Context, id int64) (reduce.State, bool, error) {
	c.gets++
	return c.Store.Get(ctx, id)
}

func TestARowThatStaysBusyIsGivenUpOnAndSaysSo(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games",
		Sleep: func(context.Context, time.Duration) error { return nil }}
	ctx := context.Background()
	_ = h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json"))
	stuck := &conflictN{Store: st, n: 1000}
	h.Store = stuck
	err := h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json"))
	if !errors.Is(err, gamestore.ErrConflict) {
		t.Fatalf("err = %v, want the conflict handed back so Lambda retries the event later", err)
	}
	if stuck.puts != maxConflictAttempts {
		t.Errorf("puts = %d, want exactly %d", stuck.puts, maxConflictAttempts)
	}
}

func TestOtherErrorsAreNotRetried(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: &failingPut{Store: st}, Pub: pub, TopicPrefix: "hockeytrack/games",
		Sleep: func(context.Context, time.Duration) error {
			t.Error("paused for an error that is not a conflict")
			return nil
		}}
	if err := h.Handle(context.Background(), ebEvent(t, "nhl.game.status", "status_live.json")); err == nil {
		t.Error("a failed write was swallowed")
	}
}

type failingPut struct{ gamestore.Store }

func (failingPut) Put(context.Context, reduce.State) error { return errors.New("throttled") }

func TestACancelledInvocationStopsRetrying(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: &conflictN{Store: st, n: 1000}, Pub: pub, TopicPrefix: "hockeytrack/games",
		Sleep: func(ctx context.Context, _ time.Duration) error { return context.Canceled }}
	if err := h.Handle(context.Background(), ebEvent(t, "nhl.game.status", "status_live.json")); !errors.Is(err, context.Canceled) {
		t.Errorf("err = %v, want the cancellation", err)
	}
}
