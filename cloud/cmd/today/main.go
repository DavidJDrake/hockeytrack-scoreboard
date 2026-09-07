// Command today publishes, every ten minutes, the list of games the
// scoreboard's owner can pick from: today's games plus yesterday's
// unfinished ones until noon ET.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"
	_ "time/tzdata"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/today"
)

const topic = "hockeytrack/games/today"

func run(ctx context.Context, scheduleURL string, store gamestore.Store, pub iotpub.Publisher) error {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, scheduleURL, nil)
	req.Header.Set("User-Agent", "hockeytrack-scoreboard/1.0 (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("schedule: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	active, err := store.ListActive(ctx)
	if err != nil {
		return err
	}
	states := map[int64]string{}
	for _, s := range active {
		states[s.GameID] = s.GameState
	}
	doc, err := today.Build(body, states, time.Now())
	if err != nil {
		return err
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	return pub.Publish(ctx, topic, out, true)
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	endpoint := os.Getenv("IOT_ENDPOINT")
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	store := gamestore.NewDynamo(dynamodb.NewFromConfig(cfg), os.Getenv("GAMES_TABLE"))
	url := os.Getenv("SCHEDULE_URL") // https://hockeytrack.davidjdrake.com/data/schedule.json
	lambda.Start(func(ctx context.Context) error { return run(ctx, url, store, iotpub.NewIoT(iot)) })
}
