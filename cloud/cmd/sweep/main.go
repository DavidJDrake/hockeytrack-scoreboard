// Command sweep runs once a day and names the panels that nobody owns and
// the broker has not seen for a year (internal/sweep, SCO-33).
//
// It is dry-run only: it reads the devices table and the fleet index, logs
// what it would retire, and changes nothing. terraform/sweep.tf is the policy
// that says what it may do, and that is two reads. The retire action is
// SCO-32's, and act mode is a deliberate later step described in the
// internal/sweep package comment; this binary has no such mode and no flag
// that could turn one on.
package main

import (
	"context"
	"log/slog"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iot"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/presence"
	"hockeytrack-scoreboard/internal/sweep"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	devicesTable := os.Getenv("DEVICES_TABLE")
	if devicesTable == "" {
		slog.Error("DEVICES_TABLE is required")
		os.Exit(1)
	}
	s := &sweep.Sweep{
		Devices:  devices.NewDynamo(dynamodb.NewFromConfig(cfg), devicesTable),
		Presence: presence.NewIoT(iot.NewFromConfig(cfg)),
		Now:      time.Now,
	}
	// The scheduler's event carries nothing worth reading: there is no
	// per-panel mode and no input that could widen a run.
	lambda.Start(func(ctx context.Context) error {
		_, err := s.Run(ctx)
		return err
	})
}
