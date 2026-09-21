package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/rand"
	"strconv"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/reduce"
)

type Handler struct {
	Store       gamestore.Store
	Pub         iotpub.Publisher
	TopicPrefix string
	// Sleep pauses between attempts at a busy row. Nil means really sleep;
	// a test passes its own so it does not have to.
	Sleep func(context.Context, time.Duration) error
}

// A live game is a heartbeat every five seconds plus plays arriving in
// bursts, each its own invocation, all writing one row under an optimistic
// lock. Losing that race is ordinary. What is not acceptable is what giving
// up costs: the event goes back to Lambda, which tries again a minute later,
// then two minutes after that, then drops it -- so a penalty reaches a panel
// minutes late, or never. Measured 2026-09-20: 6 to 12 events an hour were
// given up on after a single retry.
//
// So: try again here, several times, quickly. The pause is short and random
// so that invocations which collided once do not collide again in step. The
// total is well under a second, against a function timeout of several.
const (
	maxConflictAttempts = 8
	maxConflictPause    = 120 * time.Millisecond
)

func (h Handler) pause(ctx context.Context, attempt int) error {
	// Grows with the attempt, never past the ceiling, and never the same
	// for two invocations: 1..20ms, then 1..40ms, and so on.
	ceiling := time.Duration(attempt+1) * 20 * time.Millisecond
	if ceiling > maxConflictPause {
		ceiling = maxConflictPause
	}
	d := time.Duration(rand.Int63n(int64(ceiling))) + time.Millisecond
	if h.Sleep != nil {
		return h.Sleep(ctx, d)
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

type gameIDOnly struct {
	GameID int64 `json:"gameId"`
}

// Handle folds one bus event into the game's state and republishes the
// retained document when it changed. A version conflict (another
// invocation wrote first) is retried from a fresh read, several times and
// quickly; see maxConflictAttempts.
func (h Handler) Handle(ctx context.Context, ev events.CloudWatchEvent) error {
	var id gameIDOnly
	if err := json.Unmarshal(ev.Detail, &id); err != nil || id.GameID == 0 {
		return fmt.Errorf("event %s has no gameId: %v", ev.DetailType, err)
	}
	for attempt := 0; attempt < maxConflictAttempts; attempt++ {
		if attempt > 0 {
			if err := h.pause(ctx, attempt-1); err != nil {
				return err
			}
		}
		cur, _, err := h.Store.Get(ctx, id.GameID)
		if err != nil {
			return err
		}
		next, changed, err := reduce.Reduce(cur, reduce.Event{DetailType: ev.DetailType, Detail: ev.Detail, Time: ev.Time})
		if err != nil {
			return err
		}
		if err := h.Store.Put(ctx, next); err != nil {
			if errors.Is(err, gamestore.ErrConflict) {
				// Ordinary, so not a warning: a warning an hour is noise,
				// and the one worth reading is below, when it is given up on.
				slog.Info("version conflict; retrying", "gameId", id.GameID, "attempt", attempt+1)
				continue
			}
			return err
		}
		if !changed {
			return nil
		}
		body, err := next.JSON()
		if err != nil {
			return err
		}
		topic := h.TopicPrefix + "/" + strconv.FormatInt(id.GameID, 10) + "/state"
		return h.Pub.Publish(ctx, topic, body, true)
	}
	// Handed back so that Lambda retries the event later: late is better
	// than lost. This line is the one to alarm on.
	slog.Error("gave up on a busy game row", "gameId", id.GameID, "attempts", maxConflictAttempts, "detailType", ev.DetailType)
	return gamestore.ErrConflict
}
