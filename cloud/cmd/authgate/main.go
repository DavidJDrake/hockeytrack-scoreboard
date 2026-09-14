package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	name := os.Getenv("ALLOWLIST_PARAMETER")
	if name == "" {
		slog.Error("ALLOWLIST_PARAMETER is required")
		os.Exit(1)
	}
	client := ssm.NewFromConfig(cfg)
	h := &Handler{
		Allowlist: &Allowlist{
			Fetch: func(ctx context.Context) (string, error) {
				out, err := client.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(name)})
				if err != nil {
					return "", err
				}
				if out.Parameter == nil || out.Parameter.Value == nil {
					return "", errors.New("invite list parameter has no value")
				}
				return *out.Parameter.Value, nil
			},
			TTL: time.Minute,
			Now: time.Now,
		},
		Log: slog.Default(),
	}
	lambda.Start(h.Handle)
}
