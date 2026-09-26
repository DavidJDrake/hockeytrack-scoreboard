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
	"github.com/aws/aws-sdk-go-v2/service/kms"
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

// signingKey reads the release-signing key's public half from KMS each run,
// so a swapped public key in the repository cannot fool the monitor.
type signingKey struct {
	client *kms.Client
	keyID  string
}

func (k signingKey) PublicKey(ctx context.Context) ([]byte, error) {
	out, err := k.client.GetPublicKey(ctx, &kms.GetPublicKeyInput{KeyId: aws.String(k.keyID)})
	if err != nil {
		return nil, err
	}
	return out.PublicKey, nil
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
	name, topicARN, repo, keyID := os.Getenv("IMAGES_BUCKET"), os.Getenv("TOPIC_ARN"), os.Getenv("GITHUB_REPO"), os.Getenv("SIGNING_KEY_ID")
	if name == "" || topicARN == "" || repo == "" || keyID == "" {
		slog.Error("IMAGES_BUCKET, TOPIC_ARN, GITHUB_REPO and SIGNING_KEY_ID are required")
		os.Exit(1)
	}
	objs := bucket{client: s3.NewFromConfig(cfg), name: name}
	rel := GitHub{Repo: repo, API: "https://api.github.com", Web: "https://github.com", Client: &http.Client{Timeout: 30 * time.Second, CheckRedirect: redirectPolicy}}
	n := topic{client: sns.NewFromConfig(cfg), arn: topicARN}
	signing := &Signing{Assets: rel, Key: signingKey{client: kms.NewFromConfig(cfg), keyID: keyID}, Now: time.Now}
	lambda.Start(func(ctx context.Context) error { return Run(ctx, objs, rel, n, signing) })
}
