package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/reduce"
)

type Handler struct {
	Store       gamestore.Store
	Pub         iotpub.Publisher
	TopicPrefix string
}

type gameIDOnly struct {
	GameID int64 `json:"gameId"`
}

// Handle folds one bus event into the game's state and republishes the
// retained document when it changed. A version conflict (another
// invocation wrote first) is retried once from a fresh read.
func (h Handler) Handle(ctx context.Context, ev events.CloudWatchEvent) error {
	var id gameIDOnly
	if err := json.Unmarshal(ev.Detail, &id); err != nil || id.GameID == 0 {
		return fmt.Errorf("event %s has no gameId: %v", ev.DetailType, err)
	}
	for attempt := 0; attempt < 2; attempt++ {
		cur, _, err := h.Store.Get(ctx, id.GameID)
		if err != nil {
			return err
		}
		next, changed, err := reduce.Reduce(cur, reduce.Event{DetailType: ev.DetailType, Detail: ev.Detail, Time: ev.Time})
		if err != nil {
			return err
		}
		if err := h.Store.Put(ctx, next); err != nil {
			if errors.Is(err, gamestore.ErrConflict) && attempt == 0 {
				slog.Warn("version conflict; retrying", "gameId", id.GameID)
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
	return gamestore.ErrConflict
}
