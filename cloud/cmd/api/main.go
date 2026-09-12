// Command api serves the scoreboard admin API behind an API Gateway HTTP API
// with a Cognito JWT authorizer. The authorizer proves the caller signed in;
// this process decides what they may touch.
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

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/iotpub"
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
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	h := &Handler{
		Store: devices.NewDynamo(dynamodb.NewFromConfig(cfg), table),
		Pub:   iotpub.NewIoT(iot),
		Games: func(ctx context.Context) ([]byte, error) { return games(ctx, scheduleURL) },
	}
	lambda.Start(h.Handle)
}
