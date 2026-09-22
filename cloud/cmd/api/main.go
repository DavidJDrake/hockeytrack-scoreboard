// Command api serves the scoreboard admin API behind an API Gateway HTTP API
// with a Cognito JWT authorizer. This process verifies the caller's ID token
// again itself (internal/idtoken), because an event can reach it without
// passing the authorizer, and decides what they may touch.
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
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"
	lambdasvc "github.com/aws/aws-sdk-go-v2/service/lambda"
	lambdatypes "github.com/aws/aws-sdk-go-v2/service/lambda/types"

	"hockeytrack-scoreboard/internal/accounts"
	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/idtoken"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/season"
	"hockeytrack-scoreboard/internal/today"
)

// games fetches the public schedule and returns the day's games as JSON. The
// browser cannot read the schedule itself: it is served from another origin
// with no CORS headers (spec §3.5). States are empty because this endpoint
// only lists what is on today, not who is winning.
func games(ctx context.Context, scheduleURL string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, scheduleURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "hockeytrack-scoreboard/1.0 (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("schedule: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	doc, err := today.Build(body, map[int64]string{}, time.Now())
	if err != nil {
		return nil, err
	}
	return json.Marshal(doc)
}

// fetchSchedule reads HockeyTrack's schedule file. The address comes from
// this function's configuration and from nowhere else: no request reaches it.
func fetchSchedule(ctx context.Context, scheduleURL string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, scheduleURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "hockeytrack-scoreboard/1.0 (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	// Six seconds, inside this function's ten: a slow source must produce an
	// answer the site can show, not a timeout it cannot explain.
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

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	table, endpoint, scheduleURL := os.Getenv("DEVICES_TABLE"), os.Getenv("IOT_ENDPOINT"), os.Getenv("SCHEDULE_URL")
	if table == "" || endpoint == "" || scheduleURL == "" {
		slog.Error("DEVICES_TABLE, IOT_ENDPOINT and SCHEDULE_URL are required")
		os.Exit(1)
	}
	pool, client := os.Getenv("USER_POOL_ID"), os.Getenv("APP_CLIENT_ID")
	if pool == "" || client == "" {
		slog.Error("USER_POOL_ID and APP_CLIENT_ID are required")
		os.Exit(1)
	}
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	db := dynamodb.NewFromConfig(cfg)
	h := &Handler{
		Store:  devices.NewDynamo(db, table),
		Pub:    iotpub.NewIoT(iot),
		Games:  func(ctx context.Context) ([]byte, error) { return games(ctx, scheduleURL) },
		Tokens: idtoken.New(cfg.Region, pool, client),
	}
	seasons := &season.Cache{
		Fetch:    func(ctx context.Context) ([]byte, error) { return fetchSchedule(ctx, scheduleURL) },
		Now:      time.Now,
		FreshFor: 10 * time.Minute,
		StaleFor: 6 * time.Hour,
		Dropped:  func(n int) { slog.Warn("schedule rows failed a check and were left out", "rows", n) },
	}
	h.Season = seasons.Get
	// Optional, so a deployment without it still lists panels. This role may
	// only GetItem on the games table: it reads what the reducer wrote and
	// can change none of it.
	// Optional in the same way: without it nobody has account defaults and
	// the route that would save them says so.
	if accountsTable := os.Getenv("ACCOUNTS_TABLE"); accountsTable != "" {
		h.Accounts = accounts.NewDynamo(db, accountsTable)
	}
	if gamesTable := os.Getenv("GAMES_TABLE"); gamesTable != "" {
		h.Game = gamestore.NewDynamo(db, gamesTable).Get
	}
	// Optional too: without it a saved schedule is acted on within the
	// minute. Asynchronous, so a slow director never holds a save; this role
	// may invoke that one function and nothing else (admin.tf).
	if fn := os.Getenv("DIRECTOR_FUNCTION"); fn != "" {
		client := lambdasvc.NewFromConfig(cfg)
		h.Direct = func(ctx context.Context, thing string) error {
			payload, err := json.Marshal(struct {
				Thing string `json:"thing"`
			}{thing})
			if err != nil {
				return err
			}
			_, err = client.Invoke(ctx, &lambdasvc.InvokeInput{
				FunctionName:   aws.String(fn),
				InvocationType: lambdatypes.InvocationTypeEvent,
				Payload:        payload,
			})
			return err
		}
	}
	lambda.Start(h.Handle)
}
