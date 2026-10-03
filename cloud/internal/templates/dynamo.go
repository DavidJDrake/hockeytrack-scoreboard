package templates

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

// Dynamo is the DynamoDB-backed Store. Items carry owner (S, the hash key),
// templateId (S, the range key), name (S) and games (S, the ids as a JSON
// array). The key is what enforces the package's rule: a Query under the
// owner is the only read, and a write names both halves of the key, so an
// id from a client can only ever reach the caller's own partition. No index,
// so there is no second way in. Query, PutItem and DeleteItem are the only
// actions the API role holds on this table (terraform/admin.tf); the
// director's role holds Query alone (terraform/director.tf).
type Dynamo struct {
	client *dynamodb.Client
	table  string
}

// NewDynamo returns a Store backed by the given table.
func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table}
}

func key(owner, id string) map[string]types.AttributeValue {
	return map[string]types.AttributeValue{
		"owner":      &types.AttributeValueMemberS{Value: owner},
		"templateId": &types.AttributeValueMemberS{Value: id},
	}
}

// marshalTemplate writes a Template as an item under owner.
func marshalTemplate(owner string, t Template) map[string]types.AttributeValue {
	item := key(owner, t.ID)
	item["name"] = &types.AttributeValueMemberS{Value: t.Name}
	item["games"] = &types.AttributeValueMemberS{Value: storedGames(t.Games)}
	return item
}

// unmarshalTemplate reads a Template back out of an item. The owner is not
// read back: the caller queried under it, and nothing downstream is keyed by
// it.
func unmarshalTemplate(item map[string]types.AttributeValue) (Template, error) {
	t := Template{}
	var err error
	if t.ID, err = attrS(item, "templateId"); err != nil {
		return Template{}, err
	}
	if t.Name, err = attrS(item, "name"); err != nil {
		return Template{}, err
	}
	// A damaged games attribute reads as an empty template, the way a
	// damaged schedule reads as nothing asked for: it must not take the
	// account's other templates off the list, and an empty template is what a
	// panel shows less for, never more.
	t.Games = []int64{}
	if attr, ok := item["games"].(*types.AttributeValueMemberS); ok {
		t.Games = loadGames(attr.Value)
	}
	return t, nil
}

func storedGames(ids []int64) string {
	if ids == nil {
		ids = []int64{}
	}
	b, err := json.Marshal(ids)
	if err != nil {
		return "[]"
	}
	return string(b)
}

func loadGames(stored string) []int64 {
	var ids []int64
	if err := json.Unmarshal([]byte(stored), &ids); err != nil || len(ids) > MaxGames || !distinctPositive(ids) {
		return []int64{}
	}
	return ids
}

func attrS(item map[string]types.AttributeValue, k string) (string, error) {
	av, ok := item[k]
	if !ok {
		return "", fmt.Errorf("templates: item is missing %q", k)
	}
	s, ok := av.(*types.AttributeValueMemberS)
	if !ok {
		return "", fmt.Errorf("templates: %q attribute is not a string", k)
	}
	return s.Value, nil
}

// List queries under the owner and nothing else. Consistent, because the
// API reads its own writes (a create followed by a list) and an owner's
// twenty rows are not worth a stale answer.
func (x *Dynamo) List(ctx context.Context, owner string) ([]Template, error) {
	var out []Template
	var start map[string]types.AttributeValue
	for {
		res, err := x.client.Query(ctx, &dynamodb.QueryInput{
			TableName:                 aws.String(x.table),
			KeyConditionExpression:    aws.String("#o = :o"),
			ExpressionAttributeNames:  map[string]string{"#o": "owner"},
			ExpressionAttributeValues: map[string]types.AttributeValue{":o": &types.AttributeValueMemberS{Value: owner}},
			ConsistentRead:            aws.Bool(true),
			ExclusiveStartKey:         start,
		})
		if err != nil {
			return nil, err
		}
		for _, item := range res.Items {
			t, err := unmarshalTemplate(item)
			if err != nil {
				return nil, err
			}
			out = append(out, t)
		}
		if start = res.LastEvaluatedKey; len(start) == 0 {
			if out == nil {
				out = []Template{}
			}
			return sorted(out), nil
		}
	}
}

func (x *Dynamo) Create(ctx context.Context, owner string, t Template) error {
	_, err := x.client.PutItem(ctx, &dynamodb.PutItemInput{
		TableName:           aws.String(x.table),
		Item:                marshalTemplate(owner, t),
		ConditionExpression: aws.String("attribute_not_exists(templateId)"),
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return ErrExists
	}
	return err
}

func (x *Dynamo) Replace(ctx context.Context, owner string, t Template) error {
	// PutItem addresses the item by both halves of the key, and the
	// condition asks that an item already be there, so this can only
	// overwrite a row the owner already holds; a client-chosen id never
	// creates one.
	_, err := x.client.PutItem(ctx, &dynamodb.PutItemInput{
		TableName:           aws.String(x.table),
		Item:                marshalTemplate(owner, t),
		ConditionExpression: aws.String("attribute_exists(templateId)"),
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return ErrNotFound
	}
	return err
}

func (x *Dynamo) Delete(ctx context.Context, owner, id string) error {
	_, err := x.client.DeleteItem(ctx, &dynamodb.DeleteItemInput{
		TableName:           aws.String(x.table),
		Key:                 key(owner, id),
		ConditionExpression: aws.String("attribute_exists(templateId)"),
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return ErrNotFound
	}
	return err
}
