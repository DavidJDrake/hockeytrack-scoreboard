// Package iotpub republishes game state to AWS IoT Core as retained MQTT
// messages.
package iotpub

import (
	"context"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"
)

// Publisher publishes payload to topic. retain controls whether the broker
// keeps the message as the topic's last-known value for new subscribers.
type Publisher interface {
	Publish(ctx context.Context, topic string, payload []byte, retain bool) error
}

// Message is one recorded publish, captured by Fake.
type Message struct {
	Topic   string
	Payload []byte
	Retain  bool
}

// Fake is an in-memory Publisher for tests.
type Fake struct {
	mu       sync.Mutex
	Messages []Message
}

func (f *Fake) Publish(_ context.Context, topic string, payload []byte, retain bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Messages = append(f.Messages, Message{Topic: topic, Payload: append([]byte(nil), payload...), Retain: retain})
	return nil
}

// IoT is a Publisher backed by AWS IoT Data Plane.
type IoT struct{ client *iotdataplane.Client }

// NewIoT returns an IoT publisher backed by client.
func NewIoT(client *iotdataplane.Client) *IoT { return &IoT{client: client} }

func (p *IoT) Publish(ctx context.Context, topic string, payload []byte, retain bool) error {
	_, err := p.client.Publish(ctx, &iotdataplane.PublishInput{
		Topic:       aws.String(topic),
		Payload:     payload,
		Qos:         1,
		Retain:      retain,
		ContentType: aws.String("application/json"),
	})
	return err
}
