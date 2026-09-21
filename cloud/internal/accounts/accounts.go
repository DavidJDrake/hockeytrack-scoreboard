// Package accounts keeps what belongs to an account rather than to a panel:
// today, the owner's default display settings. Keyed by the Cognito subject
// and by nothing else, so there is no way to ask for somebody else's row.
package accounts

import (
	"context"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"

	"hockeytrack-scoreboard/internal/settings"
)

type Store interface {
	// Defaults returns the account's default settings. An account with no
	// row has none, which is not an error.
	Defaults(ctx context.Context, owner string) (settings.Settings, error)
	SetDefaults(ctx context.Context, owner string, s settings.Settings) error
}

type Fake struct {
	mu    sync.Mutex
	items map[string]settings.Settings
	Err   error // returned by every call when set
}

func NewFake() *Fake { return &Fake{items: map[string]settings.Settings{}} }

func (f *Fake) Defaults(_ context.Context, owner string) (settings.Settings, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.items[owner], f.Err
}

func (f *Fake) SetDefaults(_ context.Context, owner string, s settings.Settings) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return f.Err
	}
	f.items[owner] = s
	return nil
}

// Dynamo stores one item per account: owner (S, the hash key) and display
// (S, the settings as JSON). GetItem and UpdateItem only; the role that runs
// this needs nothing else on the table.
type Dynamo struct {
	client *dynamodb.Client
	table  string
}

func NewDynamo(client *dynamodb.Client, table string) *Dynamo { return &Dynamo{client, table} }

func key(owner string) map[string]types.AttributeValue {
	return map[string]types.AttributeValue{"owner": &types.AttributeValueMemberS{Value: owner}}
}

func (d *Dynamo) Defaults(ctx context.Context, owner string) (settings.Settings, error) {
	out, err := d.client.GetItem(ctx, &dynamodb.GetItemInput{TableName: aws.String(d.table), Key: key(owner), ConsistentRead: aws.Bool(true)})
	if err != nil {
		return settings.Settings{}, err
	}
	if attr, ok := out.Item["display"].(*types.AttributeValueMemberS); ok {
		return settings.Load(attr.Value), nil
	}
	return settings.Settings{}, nil
}

func (d *Dynamo) SetDefaults(ctx context.Context, owner string, s settings.Settings) error {
	_, err := d.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                 aws.String(d.table),
		Key:                       key(owner),
		UpdateExpression:          aws.String("SET display = :d"),
		ExpressionAttributeValues: map[string]types.AttributeValue{":d": &types.AttributeValueMemberS{Value: settings.Stored(s)}},
	})
	return err
}
