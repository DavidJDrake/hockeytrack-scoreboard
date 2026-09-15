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
	"hockeytrack-scoreboard/internal/enroll"
	"hockeytrack-scoreboard/internal/idtoken"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	enrollments := os.Getenv("ENROLLMENTS_TABLE")
	deviceTable := os.Getenv("DEVICES_TABLE")
	endpoint := os.Getenv("IOT_ENDPOINT")
	policy := os.Getenv("DEVICE_POLICY")
	if enrollments == "" || deviceTable == "" || endpoint == "" || policy == "" {
		slog.Error("ENROLLMENTS_TABLE, DEVICES_TABLE, IOT_ENDPOINT and DEVICE_POLICY are required")
		os.Exit(1)
	}
	pool, client := os.Getenv("USER_POOL_ID"), os.Getenv("APP_CLIENT_ID")
	if pool == "" || client == "" {
		slog.Error("USER_POOL_ID and APP_CLIENT_ID are required")
		os.Exit(1)
	}
	ddb := dynamodb.NewFromConfig(cfg)
	h := &Handler{
		Enrollments: enroll.NewDynamo(ddb, enrollments),
		Devices:     devices.NewDynamo(ddb, deviceTable),
		Issuer:      enroll.NewIoTIssuer(iot.NewFromConfig(cfg), policy),
		IoTEndpoint: endpoint,
		TTL:         24 * time.Hour,
		CodeTTL:     15 * time.Minute,
		Tokens:      idtoken.New(cfg.Region, pool, client),
	}
	lambda.Start(h.Handle)
}
