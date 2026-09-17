package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/aws-sdk-go-v2/service/sns"
)

type bucket struct {
	client *s3.Client
	name   string
}

func (b bucket) Get(ctx context.Context, key string) (io.ReadCloser, error) {
	out, err := b.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(b.name), Key: aws.String(key)})
	var missing *types.NoSuchKey
	if errors.As(err, &missing) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return out.Body, nil
}

type topic struct {
	client *sns.Client
	arn    string
}

func (t topic) Notify(ctx context.Context, subject, message string) error {
	_, err := t.client.Publish(ctx, &sns.PublishInput{TopicArn: aws.String(t.arn), Subject: aws.String(subject), Message: aws.String(message)})
	return err
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	name, topicARN, repo := os.Getenv("IMAGES_BUCKET"), os.Getenv("TOPIC_ARN"), os.Getenv("GITHUB_REPO")
	if name == "" || topicARN == "" || repo == "" {
		slog.Error("IMAGES_BUCKET, TOPIC_ARN and GITHUB_REPO are required")
		os.Exit(1)
	}
	objs := bucket{client: s3.NewFromConfig(cfg), name: name}
	rel := GitHub{Repo: repo, API: "https://api.github.com", Web: "https://github.com", Client: &http.Client{Timeout: 30 * time.Second}}
	n := topic{client: sns.NewFromConfig(cfg), arn: topicARN}
	lambda.Start(func(ctx context.Context) error { return Run(ctx, objs, rel, n) })
}
