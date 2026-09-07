package main

import (
	"context"
	"log/slog"
	"os"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	table := os.Getenv("GAMES_TABLE")
	endpoint := os.Getenv("IOT_ENDPOINT") // https://xxxx-ats.iot.us-east-1.amazonaws.com
	if table == "" || endpoint == "" {
		slog.Error("GAMES_TABLE and IOT_ENDPOINT are required")
		os.Exit(1)
	}
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	h := Handler{
		Store:       gamestore.NewDynamo(dynamodb.NewFromConfig(cfg), table),
		Pub:         iotpub.NewIoT(iot),
		TopicPrefix: "hockeytrack/games",
	}
	lambda.Start(h.Handle)
}
