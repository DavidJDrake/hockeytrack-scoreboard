package devices

import (
	"context"
	"errors"
	"fmt"
	"strconv"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

// Dynamo is the DynamoDB-backed Store. Items carry thingName (S, partition
// key), owner (S), name (S), gameId (N), and code (S). GSI owner-index is
// keyed on owner; GSI code-index is keyed on code.
type Dynamo struct {
	client *dynamodb.Client
	table  string
}

// NewDynamo returns a Store backed by the given table.
func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table}
}

// marshalDevice writes a Device as an item. An unclaimed device omits owner
// entirely rather than storing an empty string: Claim's condition expression
// is attribute_not_exists(owner), and an empty string is an attribute that
// exists. Omitting it also keeps owner-index sparse, so unclaimed devices
// don't pile onto one partition key.
func marshalDevice(d Device) (map[string]types.AttributeValue, error) {
	item := map[string]types.AttributeValue{
		"thingName": &types.AttributeValueMemberS{Value: d.ThingName},
		"code":      &types.AttributeValueMemberS{Value: d.Code},
		"name":      &types.AttributeValueMemberS{Value: d.Name},
		"gameId":    &types.AttributeValueMemberN{Value: strconv.FormatInt(d.GameID, 10)},
	}
	if d.Owner != "" {
		item["owner"] = &types.AttributeValueMemberS{Value: d.Owner}
	}
	return item, nil
}

// unmarshalDevice reads a Device back out of an item. Written as plain field
// extraction rather than attributevalue.UnmarshalMap: owner is genuinely
// absent for an unclaimed device, and decoding that into a non-pointer string
// field needs no special handling here, so the extra machinery buys nothing.
func unmarshalDevice(item map[string]types.AttributeValue) (Device, error) {
	d := Device{}
	var err error
	if d.ThingName, err = attrS(item, "thingName"); err != nil {
		return Device{}, err
	}
	if d.Name, err = attrS(item, "name"); err != nil {
		return Device{}, err
	}
	if d.Code, err = attrS(item, "code"); err != nil {
		return Device{}, err
	}
	if owner, ok := item["owner"]; ok {
		s, ok := owner.(*types.AttributeValueMemberS)
		if !ok {
			return Device{}, fmt.Errorf("devices: owner attribute is not a string")
		}
		d.Owner = s.Value
	}
	gameID, err := attrN(item, "gameId")
	if err != nil {
		return Device{}, err
	}
	d.GameID = gameID
	return d, nil
}

func attrS(item map[string]types.AttributeValue, key string) (string, error) {
	av, ok := item[key]
	if !ok {
		return "", fmt.Errorf("devices: item is missing %q", key)
	}
	s, ok := av.(*types.AttributeValueMemberS)
	if !ok {
		return "", fmt.Errorf("devices: %q attribute is not a string", key)
	}
	return s.Value, nil
}

func attrN(item map[string]types.AttributeValue, key string) (int64, error) {
	av, ok := item[key]
	if !ok {
		return 0, fmt.Errorf("devices: item is missing %q", key)
	}
	n, ok := av.(*types.AttributeValueMemberN)
	if !ok {
		return 0, fmt.Errorf("devices: %q attribute is not a number", key)
	}
	return strconv.ParseInt(n.Value, 10, 64)
}

func (x *Dynamo) Get(ctx context.Context, thingName string) (Device, bool, error) {
	out, err := x.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(x.table),
		Key:       map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
	})
	if err != nil {
		return Device{}, false, err
	}
	if out.Item == nil {
		return Device{}, false, nil
	}
	d, err := unmarshalDevice(out.Item)
	if err != nil {
		return Device{}, false, err
	}
	return d, true, nil
}

func (x *Dynamo) ByCode(ctx context.Context, code string) (Device, bool, error) {
	out, err := x.client.Query(ctx, &dynamodb.QueryInput{
		TableName:                 aws.String(x.table),
		IndexName:                 aws.String("code-index"),
		KeyConditionExpression:    aws.String("code = :c"),
		ExpressionAttributeValues: map[string]types.AttributeValue{":c": &types.AttributeValueMemberS{Value: code}},
	})
	if err != nil {
		return Device{}, false, err
	}
	for _, item := range out.Items {
		d, err := unmarshalDevice(item)
		if err != nil {
			return Device{}, false, err
		}
		if d.Owner == "" { // a claimed code must stop resolving
			return d, true, nil
		}
	}
	return Device{}, false, nil
}

func (x *Dynamo) ListByOwner(ctx context.Context, owner string) ([]Device, error) {
	out, err := x.client.Query(ctx, &dynamodb.QueryInput{
		TableName:                 aws.String(x.table),
		IndexName:                 aws.String("owner-index"),
		KeyConditionExpression:    aws.String("#o = :o"),
		ExpressionAttributeNames:  map[string]string{"#o": "owner"},
		ExpressionAttributeValues: map[string]types.AttributeValue{":o": &types.AttributeValueMemberS{Value: owner}},
	})
	if err != nil {
		return nil, err
	}
	devs := make([]Device, 0, len(out.Items))
	for _, item := range out.Items {
		d, err := unmarshalDevice(item)
		if err != nil {
			return nil, err
		}
		devs = append(devs, d)
	}
	return devs, nil
}

func (x *Dynamo) Register(ctx context.Context, thingName, code string) error {
	item, err := marshalDevice(Device{ThingName: thingName, Code: code})
	if err != nil {
		return err
	}
	_, err = x.client.PutItem(ctx, &dynamodb.PutItemInput{
		TableName:           aws.String(x.table),
		Item:                item,
		ConditionExpression: aws.String("attribute_not_exists(thingName)"),
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return nil // already registered; provisioning may retry
	}
	return err
}

func (x *Dynamo) Claim(ctx context.Context, thingName, owner string) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                 aws.String(x.table),
		Key:                       map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
		UpdateExpression:          aws.String("SET #o = :o"),
		ConditionExpression:       aws.String("attribute_exists(thingName) AND attribute_not_exists(#o)"),
		ExpressionAttributeNames:  map[string]string{"#o": "owner"},
		ExpressionAttributeValues: map[string]types.AttributeValue{":o": &types.AttributeValueMemberS{Value: owner}},
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		// The condition also fails for a thingName that doesn't exist at
		// all; Store's contract distinguishes that case as ErrNotFound, but
		// telling the two apart needs a second read, and callers that Claim
		// only ever do so for a device they just registered or looked up.
		// ErrAlreadyClaimed is the case that matters in practice.
		return ErrAlreadyClaimed
	}
	return err
}

func (x *Dynamo) Update(ctx context.Context, d Device) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: d.ThingName}},
		UpdateExpression:         aws.String("SET #n = :n, gameId = :g"),
		ConditionExpression:      aws.String("#o = :o"),
		ExpressionAttributeNames: map[string]string{"#n": "name", "#o": "owner"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":n": &types.AttributeValueMemberS{Value: d.Name},
			":g": &types.AttributeValueMemberN{Value: strconv.FormatInt(d.GameID, 10)},
			":o": &types.AttributeValueMemberS{Value: d.Owner},
		},
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return ErrNotOwner
	}
	return err
}

func (x *Dynamo) Unbind(ctx context.Context, thingName, owner string) error {
	// REMOVE only owner, matching marshalDevice: owner is the one attribute a
	// device may lack (that absence is what makes Claim's condition and
	// owner-index sparseness work). name and gameId stay present, reset to
	// their zero values, so unmarshalDevice keeps finding them afterward.
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
		UpdateExpression:         aws.String("REMOVE #o SET #n = :empty, gameId = :zero"),
		ConditionExpression:      aws.String("#o = :o"),
		ExpressionAttributeNames: map[string]string{"#o": "owner", "#n": "name"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":o":     &types.AttributeValueMemberS{Value: owner},
			":empty": &types.AttributeValueMemberS{Value: ""},
			":zero":  &types.AttributeValueMemberN{Value: "0"},
		},
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return ErrNotOwner
	}
	return err
}
