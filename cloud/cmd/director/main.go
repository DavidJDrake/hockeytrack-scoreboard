// Command director works out, once a minute and when a schedule is saved,
// which of each panel's kept games is current and sends it
// (internal/director, design section 6).
//
// It is the second principal that may publish a panel's config document;
// terraform/director.tf is the policy that says what it may do, and it is as
// little as the job needs: read three tables, write one marker, publish to
// scoreboard/*/config. It takes one optional input, a thing name, which only
// selects a row; nothing in the event reaches a panel.
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

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"

	"hockeytrack-scoreboard/internal/accounts"
	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/director"
	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/season"
)

// fetchSchedule reads HockeyTrack's schedule file, as cmd/api does. The
// address comes from this function's configuration and from nowhere else.
func fetchSchedule(ctx context.Context, scheduleURL string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, scheduleURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "hockeytrack-scoreboard/1.0 (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	resp, err := (&http.Client{Timeout: 6 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("schedule: status %d", resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 8<<20))
}

// thingIn reads the one thing an event may say. The scheduler sends no body
// worth reading and the API sends {"thing": name}; anything else is a sweep
// of every panel, because a shape this code did not expect must not stop
// the minute from happening. The name is data: Run treats it as a row key
// and refuses a row nobody owns.
func thingIn(payload json.RawMessage) string {
	var ev struct {
		Thing string `json:"thing"`
	}
	if err := json.Unmarshal(payload, &ev); err != nil {
		return ""
	}
	return ev.Thing
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	devicesTable, accountsTable, gamesTable := os.Getenv("DEVICES_TABLE"), os.Getenv("ACCOUNTS_TABLE"), os.Getenv("GAMES_TABLE")
	endpoint, scheduleURL := os.Getenv("IOT_ENDPOINT"), os.Getenv("SCHEDULE_URL")
	if devicesTable == "" || accountsTable == "" || gamesTable == "" || endpoint == "" || scheduleURL == "" {
		slog.Error("DEVICES_TABLE, ACCOUNTS_TABLE, GAMES_TABLE, IOT_ENDPOINT and SCHEDULE_URL are required")
		os.Exit(1)
	}
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	db := dynamodb.NewFromConfig(cfg)
	seasons := &season.Cache{
		Fetch:    func(ctx context.Context) ([]byte, error) { return fetchSchedule(ctx, scheduleURL) },
		Now:      time.Now,
		FreshFor: 10 * time.Minute,
		StaleFor: 6 * time.Hour,
		Dropped:  func(n int) { slog.Warn("schedule rows failed a check and were left out", "rows", n) },
	}
	d := &director.Director{
		Devices:  devices.NewDynamo(db, devicesTable),
		Games:    gamestore.NewDynamo(db, gamesTable),
		Accounts: accounts.NewDynamo(db, accountsTable),
		Season:   seasons.Get,
		Pub:      iotpub.NewIoT(iot),
		Now:      time.Now,
	}
	lambda.Start(func(ctx context.Context, payload json.RawMessage) error {
		return d.Run(ctx, thingIn(payload))
	})
}
